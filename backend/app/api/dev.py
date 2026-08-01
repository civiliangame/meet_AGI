"""Dev harness — how the frontend gets built without a live meeting.

`POST /api/dev/harness/start` creates a meeting that replays a scripted fixture and
emits the complete real-time event stream: participants, partial and final transcript,
speaking states, wake detection, agent state transitions, and interjections with real
citations.

Nothing here requires a network connection, a Recall API key, or a Google account.

Typical loop while building the live view:

    curl -X POST localhost:8000/api/dev/harness/start \\
      -H 'content-type: application/json' \\
      -d '{"fixture_id":"q3_revenue_review","speed":6,"loop":true}'

`speed` 6-10 with `loop: true` gives you a continuously replaying meeting to develop
against. Drop to `speed: 1` to rehearse the demo at real pace.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import Field

from ..config import get_config
from ..errors import bad_request, not_found
from ..ingest import harness
from ..integrations.recall import RecallError
from ..knowledge import reload_knowledge_base
from ..pipeline import engine
from ..runtime import Runtime, get_runtime
from ..schemas import (
    Ack,
    Fixture,
    HarnessStart,
    HarnessStop,
    Meeting,
    MeetingState,
    Page,
    Schema,
    utcnow,
)
from ..store import store

router = APIRouter(prefix="/dev", tags=["dev"])


@router.get("/fixtures", response_model=Page[Fixture], summary="List available fixtures")
def list_fixtures() -> Page[Fixture]:
    return Page[Fixture](items=harness.list_fixtures(), next_cursor=None)


@router.post(
    "/harness/start",
    response_model=Meeting,
    status_code=201,
    summary="Start a fixture replay",
    description=(
        "Creates a meeting with `source: harness` and begins replaying. The event stream "
        "is identical to a real meeting — subscribe to `WS /api/meetings/{id}/live` as "
        "normal.\n\n"
        "`POST /api/meetings/{id}/speak` works on a harness meeting too. The audio goes "
        "nowhere, but the queueing, the real clip durations, and the `agent.state_changed` "
        "events are the same ones a live bot produces, so the speaking UI can be built "
        "against it."
    ),
)
async def start_harness(
    payload: HarnessStart, runtime: Runtime = Depends(get_runtime)
) -> Meeting:
    # Must be `async`: `harness.start` schedules the replay with `asyncio.create_task`,
    # and FastAPI runs sync handlers in a threadpool where there is no running loop.
    try:
        meeting = harness.start(payload.fixture_id, speed=payload.speed, loop=payload.loop)
    except FileNotFoundError as exc:
        raise bad_request(
            f"No fixture named {payload.fixture_id!r}.",
            {"available": [f.id for f in harness.list_fixtures()]},
        ) from exc

    # Give the replay a speech channel that swallows audio, so `/speak` behaves exactly
    # as it does in a live meeting minus the sound.
    runtime.attach_dry_run(meeting.id, label=f"harness:{payload.fixture_id}")
    return meeting


@router.post("/harness/stop", response_model=Meeting, summary="Stop a fixture replay")
async def stop_harness(
    payload: HarnessStop, runtime: Runtime = Depends(get_runtime)
) -> Meeting:
    meeting = harness.stop(payload.meeting_id)
    if meeting is None:
        raise not_found("Meeting", payload.meeting_id)
    await runtime.speech.detach(payload.meeting_id)
    return meeting


class TunnelStatus(Schema):
    """Whether Recall can actually reach this backend to deliver transcript."""

    configured: bool
    reachable: bool
    url: str | None = Field(default=None, description="Public webhook URL, token redacted.")
    wake_word: str
    detail: str


@router.get(
    "/tunnel",
    response_model=TunnelStatus,
    summary="Can Recall reach us?",
    description=(
        "Calls this server's own health endpoint back through `PUBLIC_BASE_URL`. That "
        "round trip is the only thing that proves the tunnel is up — a configured URL "
        "with a dead tunnel is indistinguishable from a working one until somebody says "
        "the wake word into a meeting and nothing happens."
    ),
)
async def tunnel_status() -> TunnelStatus:
    config = get_config()
    if not config.public_base_url:
        return TunnelStatus(
            configured=False,
            reachable=False,
            url=None,
            wake_word=store.settings.wake_word,
            detail="PUBLIC_BASE_URL unset — no live transcript, so the wake word cannot fire",
        )

    base = config.public_base_url.rstrip("/")
    redacted = f"{base}/api/recall/webhook/?token=***"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(
                f"{base}/api/health", headers={"ngrok-skip-browser-warning": "1"}
            )
        if response.status_code == 200:
            return TunnelStatus(
                configured=True,
                reachable=True,
                url=redacted,
                wake_word=store.settings.wake_word,
                detail=f"{base} is reachable",
            )
        detail = f"{base} answered {response.status_code} — is it pointed at this port?"
    except httpx.HTTPError as exc:
        detail = f"{base} is unreachable ({type(exc).__name__}) — is the tunnel running?"

    return TunnelStatus(
        configured=True,
        reachable=False,
        url=redacted,
        wake_word=store.settings.wake_word,
        detail=detail,
    )


class EvictedBot(Schema):
    bot_id: str
    meeting_url: str | None = None
    status: str | None = None
    left: bool = Field(description="False when Recall refused the leave call.")
    detail: str = ""


class EvictionReport(Schema):
    checked: int = Field(description="Bots Recall reported as still in a call.")
    evicted: list[EvictedBot] = Field(default_factory=list)


# A bot in any of these is still sitting in somebody's meeting.
_LIVE_BOT_STATUSES = [
    "joining_call",
    "in_waiting_room",
    "in_call_not_recording",
    "in_call_recording",
]


@router.post(
    "/evict-bots",
    response_model=EvictionReport,
    summary="Remove stray bots from every meeting",
    description=(
        "Asks Recall which bots are still in a call and tells each one to leave.\n\n"
        "A bot is a Recall-side entity: killing the backend that dispatched it does not "
        "remove it from the meeting, it just orphans it there until Recall's "
        "`everyone_left_timeout` expires. After a hard restart that leaves a silent, "
        "deaf Kindred sitting in the call next to the new one.\n\n"
        "`keep` spares a bot — normally the one the caller just dispatched."
    ),
)
async def evict_bots(
    keep: str | None = Query(default=None, description="Bot id to leave running."),
    runtime: Runtime = Depends(get_runtime),
) -> EvictionReport:
    if not runtime.recall.configured:
        return EvictionReport(checked=0, evicted=[])

    try:
        bots = await runtime.recall.list_bots(statuses=_LIVE_BOT_STATUSES)
    except RecallError as exc:
        raise bad_request(f"Could not list bots: {exc}") from exc

    evicted: list[EvictedBot] = []
    for bot in bots:
        bot_id = bot.get("id")
        if not bot_id or bot_id == keep:
            continue

        meeting_url = bot.get("meeting_url")
        if isinstance(meeting_url, dict):  # Recall returns an object on some versions
            meeting_url = meeting_url.get("meeting_id") or str(meeting_url)

        record = EvictedBot(
            bot_id=bot_id,
            meeting_url=meeting_url if isinstance(meeting_url, str) else None,
            status=runtime.recall.latest_status(bot),
            left=True,
        )
        try:
            await runtime.recall.leave_call(bot_id)
        except RecallError as exc:
            # Already gone, or Recall is unhappy. Report it rather than failing the
            # sweep — one stuck bot must not stop the rest being cleaned up.
            record.left = False
            record.detail = str(exc)[:200]
        evicted.append(record)

    # Close out any local meeting whose bot we just removed.
    for meeting in store.meetings.values():
        if meeting.bot_id and any(e.bot_id == meeting.bot_id for e in evicted):
            meeting.state = MeetingState.ENDED
            meeting.ended_at = meeting.ended_at or utcnow()

    return EvictionReport(checked=len(bots), evicted=evicted)


@router.post(
    "/reset",
    response_model=Ack,
    summary="Reset all state",
    description=(
        "Cancels running replays, clears meetings, transcripts, and interjections, and "
        "re-seeds the demo people, documents, and integrations. Also clears the "
        "pipeline's conversation memory and interjection cooldowns, and re-reads the "
        "`knowledge/` corpus from disk — so editing a document between demo runs takes "
        "effect without a restart. Handy between demo runs."
    ),
)
def reset() -> Ack:
    store.reset()
    engine.reset()
    reload_knowledge_base()
    return Ack()
