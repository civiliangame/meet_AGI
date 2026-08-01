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

from fastapi import APIRouter

from ..errors import bad_request, not_found
from ..ingest import harness
from ..schemas import Ack, Fixture, HarnessStart, HarnessStop, Meeting, Page
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
        "normal."
    ),
)
def start_harness(payload: HarnessStart) -> Meeting:
    try:
        return harness.start(payload.fixture_id, speed=payload.speed, loop=payload.loop)
    except FileNotFoundError as exc:
        raise bad_request(
            f"No fixture named {payload.fixture_id!r}.",
            {"available": [f.id for f in harness.list_fixtures()]},
        ) from exc


@router.post("/harness/stop", response_model=Meeting, summary="Stop a fixture replay")
def stop_harness(payload: HarnessStop) -> Meeting:
    meeting = harness.stop(payload.meeting_id)
    if meeting is None:
        raise not_found("Meeting", payload.meeting_id)
    return meeting


@router.post(
    "/reset",
    response_model=Ack,
    summary="Reset all state",
    description=(
        "Cancels running replays, clears meetings, transcripts, and interjections, and "
        "re-seeds the demo people, documents, and integrations. Handy between demo runs."
    ),
)
def reset() -> Ack:
    store.reset()
    return Ack()
