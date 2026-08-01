"""Live event contract — everything the server pushes over WebSocket.

`LiveEvent` is a discriminated union on `type`, which generates a proper TypeScript
discriminated union. In the frontend you can `switch (event.type)` and get exhaustive
narrowing on `event.data` for free.

**Always include a `default` branch anyway.** The backend will add event types, and an
older frontend must not crash on one it has never seen. Additive changes to this union
are considered free and will not be announced.

Two channels:
  WS /api/meetings/{id}/live   per-meeting stream
  WS /api/live                 global stream (document status, meeting lifecycle)

On connect the server sends exactly one `snapshot` carrying full current state, so the
frontend never needs a REST round-trip to initialize. Reconnect with `?since_seq=N` to
replay missed frames from a 500-frame buffer; if the requested seq has been evicted the
server sends a fresh `snapshot` instead and the client should reset its local state.

`seq` is monotonic per connection. A gap means frames were dropped — reconnect with
`since_seq` rather than trying to reconcile.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field

from .common import Schema, Ts
from .document import Document
from .interjection import Interjection
from .meeting import AgentState, Meeting, MeetingState, RosterEntry
from .transcript import TranscriptSegment

# --- Event payloads --------------------------------------------------------------


class SnapshotData(Schema):
    """Full current state. Sent exactly once, immediately on connect."""

    meeting: Meeting | None = Field(
        default=None, description="Null on the global stream when no meeting is active."
    )
    recent_segments: list[TranscriptSegment] = Field(
        default_factory=list, description="Trailing transcript window, oldest first."
    )
    interjections: list[Interjection] = Field(default_factory=list)


class MeetingStateChangedData(Schema):
    state: MeetingState
    agent_state: AgentState
    error: str | None = None


class ParticipantJoinedData(Schema):
    participant: RosterEntry


class ParticipantLeftData(Schema):
    participant_id: str


class ParticipantSpeakingChangedData(Schema):
    participant_id: str
    is_speaking: bool


class AgentStateChangedData(Schema):
    agent_state: AgentState
    detail: str | None = Field(
        default=None, description="Optional human-readable reason, e.g. 'retrieving documents'."
    )


class WakeDetectedData(Schema):
    participant_id: str
    person_id: str | None = None
    segment_id: str
    matched_text: str = Field(
        description="The span that matched the wake word, for debugging false positives.",
        examples=["Kindred, what happened to"],
    )


class QuestionCapturedData(Schema):
    question: str
    segment_ids: list[str] = Field(default_factory=list)


class ClarificationAskedData(Schema):
    question: str


class AnsweredData(Schema):
    interjection_id: str


class ErrorData(Schema):
    code: str
    message: str


# --- Event envelopes -------------------------------------------------------------


class _Envelope(Schema):
    seq: int = Field(description="Monotonic per connection. A gap means dropped frames.")
    meeting_id: str | None = None
    ts: Ts


class SnapshotEvent(_Envelope):
    type: Literal["snapshot"] = "snapshot"
    data: SnapshotData


class MeetingStateChangedEvent(_Envelope):
    type: Literal["meeting.state_changed"] = "meeting.state_changed"
    data: MeetingStateChangedData


class ParticipantJoinedEvent(_Envelope):
    type: Literal["participant.joined"] = "participant.joined"
    data: ParticipantJoinedData


class ParticipantLeftEvent(_Envelope):
    type: Literal["participant.left"] = "participant.left"
    data: ParticipantLeftData


class ParticipantSpeakingChangedEvent(_Envelope):
    type: Literal["participant.speaking_changed"] = "participant.speaking_changed"
    data: ParticipantSpeakingChangedData


class TranscriptPartialEvent(_Envelope):
    type: Literal["transcript.partial"] = "transcript.partial"
    data: TranscriptSegment


class TranscriptFinalEvent(_Envelope):
    type: Literal["transcript.final"] = "transcript.final"
    data: TranscriptSegment


class InterjectionProposedEvent(_Envelope):
    type: Literal["interjection.proposed"] = "interjection.proposed"
    data: Interjection


class InterjectionUpdatedEvent(_Envelope):
    type: Literal["interjection.updated"] = "interjection.updated"
    data: Interjection


class AgentStateChangedEvent(_Envelope):
    type: Literal["agent.state_changed"] = "agent.state_changed"
    data: AgentStateChangedData


class WakeDetectedEvent(_Envelope):
    type: Literal["speech.wake_detected"] = "speech.wake_detected"
    data: WakeDetectedData


class QuestionCapturedEvent(_Envelope):
    type: Literal["speech.question_captured"] = "speech.question_captured"
    data: QuestionCapturedData


class ClarificationAskedEvent(_Envelope):
    type: Literal["speech.clarification_asked"] = "speech.clarification_asked"
    data: ClarificationAskedData


class AnsweredEvent(_Envelope):
    type: Literal["speech.answered"] = "speech.answered"
    data: AnsweredData


class DocumentStatusChangedEvent(_Envelope):
    type: Literal["document.status_changed"] = "document.status_changed"
    data: Document


class ErrorEvent(_Envelope):
    type: Literal["error"] = "error"
    data: ErrorData


LiveEvent = Annotated[
    Union[
        SnapshotEvent,
        MeetingStateChangedEvent,
        ParticipantJoinedEvent,
        ParticipantLeftEvent,
        ParticipantSpeakingChangedEvent,
        TranscriptPartialEvent,
        TranscriptFinalEvent,
        InterjectionProposedEvent,
        InterjectionUpdatedEvent,
        AgentStateChangedEvent,
        WakeDetectedEvent,
        QuestionCapturedEvent,
        ClarificationAskedEvent,
        AnsweredEvent,
        DocumentStatusChangedEvent,
        ErrorEvent,
    ],
    Field(discriminator="type"),
]
"""Every frame the server can push. Discriminate on `type`."""


class ClientMessage(Schema):
    """The only thing the client may send.

    All mutations go over REST. Keeping the socket server-to-client only means one
    write path and no dual-write reconciliation bugs.
    """

    type: Literal["ping"] = "ping"
