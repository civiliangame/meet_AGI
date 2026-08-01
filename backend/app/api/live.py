"""Live WebSocket streams.

    WS /api/meetings/{id}/live   per-meeting
    WS /api/live                 global (document status, meeting lifecycle)

Both send exactly one `snapshot` frame on connect carrying full current state, so the
frontend never needs a REST round-trip to initialize.

Reconnect with `?since_seq=N` to replay missed frames from a 500-frame buffer. If that
point has been evicted the server sends a fresh `snapshot` instead, and the client should
discard its local state rather than trying to merge.

The socket is server-to-client only. All mutations go over REST — one write path, no
dual-write reconciliation bugs. The only accepted client frame is `{"type":"ping"}`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from ..bus import GLOBAL_CHANNEL, Channel, bus
from ..schemas import SnapshotData, utcnow
from ..store import store

log = logging.getLogger(__name__)

router = APIRouter(tags=["live"])

_RECENT_SEGMENT_WINDOW = 60
"""How much trailing transcript the snapshot carries. Enough to populate a scrolled
view without shipping a whole meeting on every reconnect."""

_PING_INTERVAL_SECONDS = 20.0


def _encode(frame: dict[str, Any]) -> dict[str, Any]:
    """Serialize a bus frame the same way FastAPI serializes REST responses.

    Going through `jsonable_encoder` is what makes the `Ts` millisecond format and
    enum-to-string conversion apply on the socket too. Without it, socket payloads
    would drift from REST payloads and the generated TypeScript would lie.
    """
    return jsonable_encoder(frame)


def _snapshot_frame(channel: Channel, meeting_id: str | None) -> dict[str, Any]:
    meeting = store.meetings.get(meeting_id) if meeting_id else None
    segments = store.segments_for(meeting_id) if meeting_id else []
    interjections = store.interjections_for(meeting_id) if meeting_id else []
    data = SnapshotData(
        meeting=meeting,
        recent_segments=[s for s in segments if s.is_final][-_RECENT_SEGMENT_WINDOW:],
        interjections=list(reversed(interjections)),
    )
    return _encode(
        {
            "type": "snapshot",
            "seq": channel.next_seq(),
            "meeting_id": meeting_id,
            "ts": utcnow(),
            "data": data,
        }
    )


async def _pump(websocket: WebSocket, channel: Channel, meeting_id: str | None, since_seq: int | None) -> None:
    sub = channel.subscribe()
    try:
        replay = channel.replay_since(since_seq) if since_seq is not None else None
        if replay is None:
            await websocket.send_json(_snapshot_frame(channel, meeting_id))
        else:
            for frame in replay:
                await websocket.send_json(_encode(frame))

        reader = asyncio.create_task(_drain_client(websocket))
        try:
            while True:
                if sub.dropped:
                    # Too far behind to catch up. Close so the client reconnects with
                    # ?since_seq= and gets a clean snapshot.
                    log.warning("closing slow subscriber on %s", channel.name)
                    await websocket.close(code=1011, reason="subscriber fell behind")
                    return
                try:
                    frame = await asyncio.wait_for(sub.get(), timeout=_PING_INTERVAL_SECONDS)
                except TimeoutError:
                    await websocket.send_json({"type": "ping"})
                    continue
                await websocket.send_json(_encode(frame))
                if reader.done():
                    return
        finally:
            reader.cancel()
    finally:
        channel.unsubscribe(sub)


async def _drain_client(websocket: WebSocket) -> None:
    """Consume client frames so disconnects are noticed promptly.

    The client is only allowed to send pings; anything else is ignored rather than
    treated as an error, so a future client feature cannot break an old server.
    """
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
    except RuntimeError:
        return


@router.websocket("/meetings/{meeting_id}/live")
async def meeting_live(
    websocket: WebSocket,
    meeting_id: str,
    since_seq: int | None = Query(default=None),
) -> None:
    await websocket.accept()
    channel = bus.channel(meeting_id)
    try:
        await _pump(websocket, channel, meeting_id, since_seq)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("meeting socket failed for %s", meeting_id)


@router.websocket("/live")
async def global_live(
    websocket: WebSocket,
    since_seq: int | None = Query(default=None),
) -> None:
    await websocket.accept()
    channel = bus.channel(GLOBAL_CHANNEL)
    try:
        await _pump(websocket, channel, None, since_seq)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("global socket failed")
