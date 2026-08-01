"""Interjection — the central object. Everything Kindred says or wants to say.

One interjection produces two artifacts, because Google Meet's chat caps messages at
500 characters:

- `chat_alert` goes into the meeting. It is a flag, not an argument. Server-enforced
  at 500 chars, targeting ~200.
- `headline` + `body_md` + `citations` go to the dashboard over WebSocket. This is
  where the reasoning actually lives.

The frontend should render `chat_alert` verbatim somewhere on the card so the operator
can see exactly what the meeting saw.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .common import Schema, Ts
from .document import Citation

CHAT_ALERT_MAX_CHARS = 500
"""Google Meet's hard limit on chat message length. Enforced server-side."""


class InterjectionKind(str, Enum):
    CONTRADICTION = "contradiction"
    """Retrieved evidence conflicts with what was said."""
    CONTEXT = "context"
    """Not a conflict — additional context worth surfacing."""
    CORRECTION = "correction"
    """A factual error with a clear right answer."""
    ANSWER = "answer"
    """A response to a direct question in speech mode."""
    CLARIFICATION = "clarification"
    """Kindred asking the human to disambiguate before answering."""


class InterjectionStatus(str, Enum):
    PROPOSED = "proposed"
    """Awaiting human approval. Only reachable under `autonomy: propose`."""
    APPROVED = "approved"
    """Approved but not yet delivered."""
    POSTED = "posted"
    """Delivered to the meeting (chat, voice, or both)."""
    DISMISSED = "dismissed"
    """Rejected by a human, or suppressed by the rate limiter."""
    FAILED = "failed"
    """Delivery attempted and failed. See `error`."""


class InterjectionTrigger(Schema):
    """What in the meeting caused this."""

    segment_ids: list[str] = Field(default_factory=list)
    person_id: str | None = None
    quote: str = Field(
        description="The utterance that triggered this, verbatim. Rendered on the card.",
        examples=["new product revenue is up about eight percent this quarter"],
    )


class Interjection(Schema):
    id: str = Field(examples=["itj_01J8XP8Q9R0S1T2U3V4W5X"])
    meeting_id: str
    kind: InterjectionKind
    status: InterjectionStatus
    trigger: InterjectionTrigger
    chat_alert: str = Field(
        max_length=CHAT_ALERT_MAX_CHARS,
        description=(
            "What was (or would be) posted to Meet chat. Hard-capped at 500 chars by "
            "the platform. Render verbatim so the operator sees what the meeting saw."
        ),
    )
    headline: str = Field(max_length=200, examples=["Sarah's revenue claim conflicts with the Q3 board deck"])
    body_md: str = Field(description="Full reasoning, markdown. The dashboard renders this.")
    confidence: float = Field(ge=0.0, le=1.0, examples=[0.82])
    citations: list[Citation] = Field(default_factory=list)
    spoken: bool = Field(
        default=False, description="True when Kindred said this out loud rather than only in chat."
    )
    error: str | None = None
    created_at: Ts
    posted_at: Ts | None = None


class InterjectionApprove(Schema):
    edited_chat_alert: str | None = Field(
        default=None,
        max_length=CHAT_ALERT_MAX_CHARS,
        description="Override the generated alert before it posts.",
    )


class InterjectionDismiss(Schema):
    reason: str | None = Field(default=None, max_length=500)


class AskRequest(Schema):
    """Type a question straight to Kindred from the dashboard.

    This is the most valuable demo-recovery tool in the API. If wake-word detection
    misfires on stage, type the question and Kindred still answers out loud.
    """

    question: str = Field(min_length=1, max_length=2000)
    speak: bool = Field(default=False, description="Also say the answer into the meeting.")
