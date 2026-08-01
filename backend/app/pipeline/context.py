"""Conversation memory — what was said earlier in this meeting.

Both loops need it, for different reasons. Speech mode needs it because questions are
rarely self-contained: "what did we say about that last quarter" is unanswerable without
the previous minute. The ambient loop needs it because half of what the user actually
wants flagged is not a document conflict at all — it is two people in the same meeting
asserting incompatible things ten minutes apart, which no amount of document retrieval
will surface.

Bounded on purpose. A window of recent turns keeps the prompt small and the latency
budget intact; a whole meeting transcript would blow both for very little gain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_WINDOW = 24
"""Turns of history kept per meeting. Roughly the last few minutes of a real meeting."""


@dataclass(frozen=True)
class Turn:
    speaker: str
    text: str
    segment_id: str
    person_id: str | None = None

    def render(self) -> str:
        return f"{self.speaker}: {self.text}"


@dataclass
class MeetingContext:
    """Rolling transcript window for one meeting."""

    window: int = DEFAULT_WINDOW
    turns: list[Turn] = field(default_factory=list)

    def add(self, turn: Turn) -> None:
        self.turns.append(turn)
        if len(self.turns) > self.window:
            del self.turns[: len(self.turns) - self.window]

    def render(self, *, exclude_segment_id: str | None = None, limit: int | None = None) -> str:
        """The window as a prompt-ready transcript, oldest first.

        `exclude_segment_id` drops the utterance currently being judged, so the prompt
        cleanly separates "what was said before" from "the claim under review" instead of
        showing the model the same sentence twice.
        """
        turns = [t for t in self.turns if t.segment_id != exclude_segment_id]
        if limit is not None:
            turns = turns[-limit:]
        return "\n".join(turn.render() for turn in turns)

    def speakers(self) -> list[str]:
        seen: list[str] = []
        for turn in self.turns:
            if turn.speaker not in seen:
                seen.append(turn.speaker)
        return seen


class ConversationMemory:
    """Per-meeting transcript windows."""

    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        self._window = window
        self._contexts: dict[str, MeetingContext] = {}

    def for_meeting(self, meeting_id: str) -> MeetingContext:
        context = self._contexts.get(meeting_id)
        if context is None:
            context = MeetingContext(window=self._window)
            self._contexts[meeting_id] = context
        return context

    def add(self, meeting_id: str, turn: Turn) -> None:
        self.for_meeting(meeting_id).add(turn)

    def clear(self, meeting_id: str) -> None:
        self._contexts.pop(meeting_id, None)

    def reset(self) -> None:
        self._contexts.clear()
