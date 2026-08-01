"""In-process pub/sub for live events.

One `Channel` per meeting, plus a global channel. Subscribers get an asyncio queue and
a replay buffer so a reconnecting client can ask for everything since a sequence number
instead of resetting its state.

Deliberately in-process: for a hackathon with one backend instance this is the whole
job. If this ever runs on more than one worker, swap the implementation for Redis
pub/sub behind the same interface — nothing outside this module knows the difference.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from .schemas import utcnow

log = logging.getLogger(__name__)

GLOBAL_CHANNEL = "__global__"
REPLAY_BUFFER_SIZE = 500
"""Frames retained for `?since_seq=` reconnects. Matches the documented contract."""

_SUBSCRIBER_QUEUE_SIZE = 1000
"""Per-subscriber backpressure. A client this far behind is dropped rather than
allowed to grow the queue without bound."""


class Subscriber:
    def __init__(self, channel_name: str) -> None:
        self.channel_name = channel_name
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self.dropped = False

    async def get(self) -> dict[str, Any]:
        return await self.queue.get()


class Channel:
    def __init__(self, name: str) -> None:
        self.name = name
        self._seq = 0
        self._subscribers: set[Subscriber] = set()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=REPLAY_BUFFER_SIZE)

    @property
    def seq(self) -> int:
        return self._seq

    def subscribe(self) -> Subscriber:
        sub = Subscriber(self.name)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)

    def replay_since(self, since_seq: int) -> list[dict[str, Any]] | None:
        """Frames after `since_seq`, or None if that point has been evicted.

        None is the signal to send a fresh snapshot instead — the client cannot
        reconstruct state from a partial replay.
        """
        if not self._buffer:
            return [] if since_seq >= self._seq else None
        earliest = self._buffer[0]["seq"]
        if since_seq + 1 < earliest:
            return None
        return [frame for frame in self._buffer if frame["seq"] > since_seq]

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def publish(self, event_type: str, data: Any, meeting_id: str | None = None) -> dict[str, Any]:
        """Build an envelope, buffer it, and fan it out to every subscriber."""
        frame = {
            "type": event_type,
            "seq": self.next_seq(),
            "meeting_id": meeting_id,
            "ts": utcnow(),
            "data": data,
        }
        self._buffer.append(frame)
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait(frame)
            except asyncio.QueueFull:
                # The client is too far behind to catch up. Mark it and let the
                # socket handler close the connection so the client reconnects
                # with ?since_seq= and gets a clean snapshot.
                sub.dropped = True
                log.warning("subscriber on %s dropped: queue full", self.name)
        return frame


class EventBus:
    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}

    def channel(self, name: str) -> Channel:
        if name not in self._channels:
            self._channels[name] = Channel(name)
        return self._channels[name]

    @property
    def global_channel(self) -> Channel:
        return self.channel(GLOBAL_CHANNEL)

    def publish_meeting(self, meeting_id: str, event_type: str, data: Any) -> dict[str, Any]:
        return self.channel(meeting_id).publish(event_type, data, meeting_id=meeting_id)

    def publish_global(
        self, event_type: str, data: Any, meeting_id: str | None = None
    ) -> dict[str, Any]:
        return self.global_channel.publish(event_type, data, meeting_id=meeting_id)

    def drop_channel(self, name: str) -> None:
        self._channels.pop(name, None)


bus = EventBus()
