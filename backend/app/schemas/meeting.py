"""Meeting — one session Meet AGI is attending.

`state` is the meeting lifecycle. `agent_state` is what Meet AGI is doing right now, and
it is the single most important field in the demo: the audience watches the pill flip to
`listening` the instant someone says "Meet AGI".

`source` distinguishes a real Recall.ai bot from a fixture replay. Every consumer of the
event stream should behave identically for both — that is the point of the harness.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .common import Schema, Ts
from .interjection import Interjection
from .transcript import TranscriptSegment


class MeetingPlatform(str, Enum):
    GOOGLE_MEET = "google_meet"
    ZOOM = "zoom"
    TEAMS = "teams"


class MeetingState(str, Enum):
    SCHEDULED = "scheduled"
    JOINING = "joining"
    IN_CALL = "in_call"
    ENDED = "ended"
    FAILED = "failed"


class AgentState(str, Enum):
    IDLE = "idle"
    """Listening ambiently. The default."""
    LISTENING = "listening"
    """Woken by the wake word, capturing a question."""
    THINKING = "thinking"
    """Retrieving and reasoning."""
    SPEAKING = "speaking"
    """Currently outputting audio into the meeting."""
    MUTED = "muted"
    """Hard override. Meet AGI will not post or speak until unmuted."""


class MeetingSource(str, Enum):
    RECALL = "recall"
    HARNESS = "harness"


class RosterEntry(Schema):
    participant_id: str = Field(
        description="Platform-scoped, stable within the meeting.", examples=["p_2"]
    )
    person_id: str | None = Field(
        default=None, description="Resolved Person, or null when unmatched."
    )
    display_name: str = Field(examples=["Sarah Chen"])
    is_host: bool = False
    matched: bool = Field(
        default=False,
        description=(
            "Whether this participant was resolved to a known Person. Unmatched "
            "participants should be visually flagged — Meet AGI has no context on them."
        ),
    )
    is_speaking: bool = False


class MeetingStats(Schema):
    """Counts for the session list. Cheap to render a row from, no extra fetches."""

    utterance_count: int = 0
    interjection_count: int = 0
    duration_seconds: int = 0
    participant_count: int = Field(default=0, description="Distinct participants seen.")
    wake_count: int = Field(default=0, description="Times Meet AGI was addressed by name.")
    source_document_count: int = Field(
        default=0, description="Distinct documents cited during this session."
    )


class Meeting(Schema):
    id: str = Field(examples=["mtg_01J8XM6N7P8Q9R0S1T2U3V"])
    title: str = Field(examples=["Q3 Revenue Review"])
    meeting_url: str | None = Field(
        default=None, examples=["https://meet.google.com/abc-defg-hij"]
    )
    platform: MeetingPlatform = MeetingPlatform.GOOGLE_MEET
    state: MeetingState
    agent_state: AgentState = AgentState.IDLE
    source: MeetingSource = MeetingSource.RECALL
    bot_id: str | None = Field(
        default=None, description="Recall.ai bot id. Null when `source` is `harness`."
    )
    roster: list[RosterEntry] = Field(default_factory=list)
    started_at: Ts | None = None
    ended_at: Ts | None = None
    stats: MeetingStats = Field(default_factory=MeetingStats)
    error: str | None = None


class MeetingCreate(Schema):
    meeting_url: str = Field(
        min_length=8,
        description="Google Meet URL. Meet AGI dispatches a bot to join.",
        examples=["https://meet.google.com/abc-defg-hij"],
    )
    title: str | None = Field(default=None, max_length=200)
    expected_person_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Who you expect to attend. Pre-seeds speaker matching, which is more reliable "
            "than matching on platform display names alone."
        ),
    )


class MuteRequest(Schema):
    muted: bool


# --- Post-meeting review ---------------------------------------------------------
# The dashboard is a place to check sources and transcript *after* a meeting, so these
# types answer "what did Meet AGI claim, and what did it read to get there?"


class SourceQuote(Schema):
    """One passage Meet AGI cited, and every claim that leaned on it.

    Deduplicated by passage rather than listed per citation: two claims citing the same
    line of a deck is one piece of evidence used twice, and rendering the identical
    quote twice on an audit screen reads as a bug. `interjection_ids` carries both.
    """

    interjection_ids: list[str] = Field(
        default_factory=list, description="Every claim that cited this passage."
    )
    chunk_id: str
    page: int | None = Field(default=None, description="1-indexed. Null for non-paginated sources.")
    quote: str = Field(description="The retrieved span, verbatim. Render as-is — it is the evidence.")
    relevance: float = Field(ge=0.0, le=1.0)


class CitedDocument(Schema):
    """A document Meet AGI actually cited during a session.

    Aggregated from the citations on that session's interjections, so this is what was
    genuinely used, not merely what was available to search. That distinction is the
    point of the review screen: it lets a human audit whether Meet AGI read the right
    thing before it spoke.
    """

    document_id: str
    filename: str
    citation_count: int = Field(description="Total citations across all interjections.")
    interjection_ids: list[str] = Field(default_factory=list)
    quotes: list[SourceQuote] = Field(default_factory=list)


class MeetingBundle(Schema):
    """Everything a review page needs, in one request.

    The alternative is four round-trips (meeting, transcript, interjections, sources)
    that all have to land before anything can render. For a page whose whole job is
    reading a finished meeting, one call is simpler on both sides.

    Live views should use the WebSocket instead — this is a snapshot, not a stream.
    """

    meeting: Meeting
    transcript: list[TranscriptSegment]
    interjections: list[Interjection]
    sources: list[CitedDocument]


# --- Dev harness -----------------------------------------------------------------
# The harness exists so the frontend can be built end to end without a live meeting,
# a Recall API key, or an internet connection. It emits the identical event stream.


class Fixture(Schema):
    id: str = Field(examples=["q3_revenue_review"])
    title: str = Field(examples=["Q3 Revenue Review"])
    description: str
    duration_seconds: int
    participant_count: int


class HarnessStart(Schema):
    fixture_id: str = Field(examples=["q3_revenue_review"])
    speed: float = Field(
        default=1.0,
        ge=0.1,
        le=50.0,
        description="Playback multiplier. Use 4-10 to iterate quickly; 1.0 to demo.",
    )
    loop: bool = Field(default=False, description="Restart the fixture when it ends.")


class HarnessStop(Schema):
    meeting_id: str
