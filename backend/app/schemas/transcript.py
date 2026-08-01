"""TranscriptSegment — one utterance.

Partials arrive at high frequency. The frontend should render `is_final: false` segments
as a single mutable "live line" per speaker and only commit to the transcript log on
`is_final: true`. Appending every partial produces a flickering, unreadable log.

Partials and their eventual final share the same `id`, so a final replaces the partial
it supersedes rather than appearing as a new entry.
"""

from __future__ import annotations

from pydantic import Field

from .common import Schema


class TranscriptSegment(Schema):
    id: str = Field(examples=["seg_01J8XN7P8Q9R0S1T2U3V4W"])
    meeting_id: str = Field(examples=["mtg_01J8XM6N7P8Q9R0S1T2U3V"])
    participant_id: str = Field(
        description="Platform-scoped participant id, stable within a meeting.",
        examples=["p_2"],
    )
    person_id: str | None = Field(
        default=None,
        description="Resolved Person, or null when the speaker could not be matched.",
        examples=["prs_01J8XK2M3N4P5Q6R7S8T9V"],
    )
    speaker_name: str = Field(
        description="Best available name: the matched Person, else the platform display name.",
        examples=["Sarah Chen"],
    )
    text: str = Field(examples=["new product revenue is up about eight percent this quarter"])
    is_final: bool = Field(
        description="False for in-progress partials. Only finals drive the reasoning pipeline."
    )
    start_ms: int = Field(ge=0, description="Offset from meeting start.", examples=[154200])
    end_ms: int = Field(ge=0, examples=[158900])
    confidence: float | None = Field(default=None, ge=0.0, le=1.0, examples=[0.94])
