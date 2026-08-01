"""Meetings, transcript access, and live agent control.

The three control endpoints (`/wake`, `/mute`, `/ask`) are stage insurance. If wake-word
detection misfires during the demo, you drive Kindred from the dashboard instead and the
audience never knows. Build them into the UI early rather than treating them as debug
tools.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from ..bus import bus
from ..errors import bad_request, not_found
from ..ids import PREFIX_CHUNK
from ..integrations.recall import RecallError
from ..runtime import Runtime, get_runtime
from ..schemas import (
    AgentState,
    AgentStateChangedData,
    AnsweredData,
    AskRequest,
    Citation,
    Interjection,
    InterjectionKind,
    InterjectionStatus,
    InterjectionTrigger,
    Meeting,
    MeetingCreate,
    MeetingSource,
    MeetingState,
    MeetingStats,
    MuteRequest,
    Page,
    QuestionCapturedData,
    TranscriptSegment,
    utcnow,
)
from ..store import DECK_DOC_ID, paginate, store

router = APIRouter(prefix="/meetings", tags=["meetings"])

_THINKING_SECONDS = 1.1
"""Simulated reasoning delay for `/ask`, so the frontend's thinking state is visible."""

_SPEAKING_SECONDS = 5.0
"""How long a *simulated* spoken answer holds `speaking`.

Only used for harness meetings, which have no audio channel. A live Recall meeting goes
through `SpeechOutput`, which holds for the clip's real duration."""


def _require(meeting_id: str) -> Meeting:
    meeting = store.meetings.get(meeting_id)
    if meeting is None:
        raise not_found("Meeting", meeting_id)
    return meeting


def _publish_state(meeting: Meeting) -> None:
    bus.publish_meeting(
        meeting.id,
        "meeting.state_changed",
        {"state": meeting.state, "agent_state": meeting.agent_state, "error": meeting.error},
    )


def _set_agent_state(meeting: Meeting, state: AgentState, detail: str | None = None) -> None:
    meeting.agent_state = state
    bus.publish_meeting(
        meeting.id,
        "agent.state_changed",
        AgentStateChangedData(agent_state=state, detail=detail),
    )


@router.get("", response_model=Page[Meeting], summary="List meetings")
def list_meetings(
    state: MeetingState | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> Page[Meeting]:
    items = sorted(store.meetings.values(), key=lambda m: m.id, reverse=True)
    if state is not None:
        items = [m for m in items if m.state == state]
    window, next_cursor = paginate(items, cursor, limit)
    return Page[Meeting](items=window, next_cursor=next_cursor)


@router.post(
    "",
    response_model=Meeting,
    status_code=201,
    summary="Send Kindred to a meeting",
    description=(
        "Dispatches a Recall.ai bot and returns immediately with the meeting in "
        "`joining`. The bot is not audible until someone admits it from the Google Meet "
        "waiting room; watch for `state: in_call` on the socket, or poll this meeting.\n\n"
        "The bot can speak from the moment it is admitted — see "
        "`POST /api/meetings/{id}/speak`. Transcript ingestion lands separately.\n\n"
        "Without `RECALL_API_KEY` set, this returns a `failed` meeting explaining how to "
        "run a fixture-backed meeting instead. Nothing crashes; the frontend can build "
        "both paths."
    ),
)
async def create_meeting(
    payload: MeetingCreate, runtime: Runtime = Depends(get_runtime)
) -> Meeting:
    for person_id in payload.expected_person_ids:
        if person_id not in store.people:
            raise bad_request(f"Unknown person {person_id}", {"person_id": person_id})

    if not runtime.recall_configured:
        # Deliberately a `failed` meeting rather than a 500: the create form, the
        # joining state, and the failure state all stay buildable without credentials.
        meeting = Meeting(
            id=store.new_meeting_id(),
            title=payload.title or "Untitled meeting",
            meeting_url=payload.meeting_url,
            state=MeetingState.FAILED,
            agent_state=AgentState.IDLE,
            source=MeetingSource.RECALL,
            roster=[],
            started_at=utcnow(),
            ended_at=utcnow(),
            stats=MeetingStats(),
            error=(
                "RECALL_API_KEY is not set, so no bot was dispatched. Set it in .env, or "
                "use POST /api/dev/harness/start for a fixture-backed meeting with the "
                "identical event stream."
            ),
        )
        store.meetings[meeting.id] = meeting
        _publish_state(meeting)
        return meeting

    # Roster stays empty until participants actually join; `expected_person_ids` only
    # pre-seeds speaker matching, it does not assert attendance.
    try:
        return await runtime.sessions.start(
            meeting_url=payload.meeting_url,
            title=payload.title or "Untitled meeting",
        )
    except RecallError as exc:
        raise bad_request(
            f"Recall rejected the bot: {exc}", {"meeting_url": payload.meeting_url}
        ) from exc


@router.get("/{meeting_id}", response_model=Meeting, summary="Get a meeting")
def get_meeting(meeting_id: str) -> Meeting:
    return _require(meeting_id)


@router.post("/{meeting_id}/leave", response_model=Meeting, summary="Make Kindred leave")
async def leave_meeting(meeting_id: str, runtime: Runtime = Depends(get_runtime)) -> Meeting:
    meeting = _require(meeting_id)
    if task := store.harness_tasks.pop(meeting_id, None):
        task.cancel()

    if runtime.sessions.get(meeting_id) is not None:
        # A real bot: pull it out of the call and close down its speech channel. The
        # session manager owns the terminal state, so return without re-publishing.
        return await runtime.sessions.leave(meeting_id)

    meeting.state = MeetingState.ENDED
    meeting.ended_at = utcnow()
    meeting.agent_state = AgentState.IDLE
    _publish_state(meeting)
    return meeting


@router.get(
    "/{meeting_id}/transcript",
    response_model=Page[TranscriptSegment],
    summary="Get the transcript",
    description="Chronological, oldest first. Finals only — partials are socket-only.",
)
def get_transcript(
    meeting_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> Page[TranscriptSegment]:
    _require(meeting_id)
    items = [s for s in store.segments_for(meeting_id) if s.is_final]
    window, next_cursor = paginate(items, cursor, limit)
    return Page[TranscriptSegment](items=window, next_cursor=next_cursor)


@router.get(
    "/{meeting_id}/interjections",
    response_model=Page[Interjection],
    summary="List interjections for a meeting",
)
def get_interjections(
    meeting_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> Page[Interjection]:
    _require(meeting_id)
    items = list(reversed(store.interjections_for(meeting_id)))
    window, next_cursor = paginate(items, cursor, limit)
    return Page[Interjection](items=window, next_cursor=next_cursor)


@router.post(
    "/{meeting_id}/wake",
    response_model=Meeting,
    summary="Wake Kindred manually",
    description=(
        "Puts Kindred into `listening` without the wake word. This is the demo safety "
        "net for wake-word false negatives — put it behind a visible button."
    ),
)
def wake(meeting_id: str) -> Meeting:
    meeting = _require(meeting_id)
    if meeting.agent_state == AgentState.MUTED:
        raise bad_request("Kindred is muted. Unmute before waking.", {"meeting_id": meeting_id})
    _set_agent_state(meeting, AgentState.LISTENING, "woken manually from the dashboard")
    return meeting


@router.post(
    "/{meeting_id}/mute",
    response_model=Meeting,
    summary="Mute or unmute Kindred",
    description="Hard override. While muted Kindred will not post to chat or speak.",
)
def mute(meeting_id: str, payload: MuteRequest) -> Meeting:
    meeting = _require(meeting_id)
    _set_agent_state(
        meeting,
        AgentState.MUTED if payload.muted else AgentState.IDLE,
        "muted by operator" if payload.muted else "unmuted by operator",
    )
    return meeting


@router.post(
    "/{meeting_id}/ask",
    response_model=Interjection,
    summary="Ask Kindred a question directly",
    description=(
        "Type a question from the dashboard. With `speak: true` Kindred also says the "
        "answer into the meeting. **Milestone 4** replaces the canned answer below with "
        "real retrieval and reasoning; the response shape is final."
    ),
)
async def ask(
    meeting_id: str, payload: AskRequest, runtime: Runtime = Depends(get_runtime)
) -> Interjection:
    meeting = _require(meeting_id)
    if meeting.agent_state == AgentState.MUTED and payload.speak:
        raise bad_request("Kindred is muted and cannot speak.", {"meeting_id": meeting_id})

    bus.publish_meeting(
        meeting_id,
        "speech.question_captured",
        QuestionCapturedData(question=payload.question, segment_ids=[]),
    )
    _set_agent_state(meeting, AgentState.THINKING, "answering a typed question")
    await asyncio.sleep(_THINKING_SECONDS)

    interjection = store.build_interjection(
        meeting_id,
        kind=InterjectionKind.ANSWER,
        status=InterjectionStatus.POSTED,
        chat_alert=(
            "\U0001f5e3️ Kindred answered a question from the dashboard. "
            "Full response in the Kindred dashboard."
        ),
        headline=f"Answer: {payload.question[:120]}",
        body_md=(
            f"**Question.** {payload.question}\n\n"
            "**Answer.** Real retrieval and reasoning land in Milestone 4. This is a "
            "placeholder response with a real citation attached so the card, the "
            "citation list, and the speaking flow can all be built and styled now.\n\n"
            "The response shape will not change when the reasoning becomes real."
        ),
        confidence=0.5,
        trigger=InterjectionTrigger(segment_ids=[], person_id=None, quote=payload.question),
        citations=[
            Citation(
                document_id=DECK_DOC_ID,
                filename="Q3-board-deck.pdf",
                chunk_id=f"{PREFIX_CHUNK}_01J8XK5B6C7D8E9F0G1H2J",
                page=14,
                quote="New Product Line: $1.42M (-12.1% MoM)",
                relevance=0.74,
            )
        ],
        spoken=payload.speak,
    )
    store.interjections_for(meeting_id).append(interjection)
    meeting.stats.interjection_count += 1

    bus.publish_meeting(meeting_id, "interjection.proposed", interjection)
    bus.publish_meeting(
        meeting_id, "speech.answered", AnsweredData(interjection_id=interjection.id)
    )

    if not payload.speak:
        _set_agent_state(meeting, AgentState.IDLE)
        return interjection

    if runtime.speech.is_attached(meeting_id):
        # Live meeting: queue real audio. `SpeechOutput` owns the speaking → idle
        # transition and holds it for the clip's actual length.
        await runtime.speech.say(meeting_id, interjection.headline)
    else:
        # Harness meeting — no bot to play into. Hold in `speaking` for roughly as long
        # as the answer would take to say, otherwise the state is emitted and overwritten
        # in the same tick and the frontend never renders it.
        _set_agent_state(meeting, AgentState.SPEAKING)
        asyncio.create_task(_return_to_idle(meeting_id, _SPEAKING_SECONDS))
    return interjection


async def _return_to_idle(meeting_id: str, after_seconds: float) -> None:
    await asyncio.sleep(after_seconds)
    meeting = store.meetings.get(meeting_id)
    # Do not stomp a mute the operator applied while Kindred was talking.
    if meeting is not None and meeting.agent_state == AgentState.SPEAKING:
        _set_agent_state(meeting, AgentState.IDLE)
