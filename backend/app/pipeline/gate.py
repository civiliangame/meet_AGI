"""The interjection rate limiter.

DESIGN.md §4: "The rate limiter is a feature, not an optimization. A copilot that won't
shut up is worse than no copilot."

Three independent reasons to stay quiet — too unsure, too soon, too many. All three are
live-tunable from the settings UI, because the right cooldown for a four-person review
is not the right cooldown for a standup and you find that out during the meeting.

Speech-mode answers are deliberately **not** gated: a human asked a direct question, so
cooldown and per-meeting caps do not apply. Only unprompted interjections are rationed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class _MeetingState:
    posted: int = 0
    last_at: float | None = None
    suppressed: int = 0


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


ALLOWED = GateDecision(True)


class InterjectionGate:
    """Decides whether an ambient interjection is allowed to reach the meeting."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._states: dict[str, _MeetingState] = {}

    def _state(self, meeting_id: str) -> _MeetingState:
        state = self._states.get(meeting_id)
        if state is None:
            state = _MeetingState()
            self._states[meeting_id] = state
        return state

    def check(self, meeting_id: str, confidence: float) -> GateDecision:
        """Whether an interjection with this confidence may be emitted now."""
        from ..store import store

        policy = store.settings.interjection
        state = self._state(meeting_id)

        if confidence < policy.min_confidence:
            return GateDecision(
                False, f"confidence {confidence:.2f} below threshold {policy.min_confidence:.2f}"
            )

        if policy.max_per_meeting and state.posted >= policy.max_per_meeting:
            return GateDecision(
                False, f"already interjected {state.posted} times this meeting"
            )

        if state.last_at is not None:
            elapsed = self._clock() - state.last_at
            if elapsed < policy.cooldown_seconds:
                remaining = policy.cooldown_seconds - elapsed
                return GateDecision(False, f"cooling down for another {remaining:.0f}s")

        return ALLOWED

    def record(self, meeting_id: str) -> None:
        """Register that an interjection was emitted. Starts the cooldown."""
        state = self._state(meeting_id)
        state.posted += 1
        state.last_at = self._clock()

    def record_suppressed(self, meeting_id: str, reason: str) -> None:
        state = self._state(meeting_id)
        state.suppressed += 1
        log.info("interjection suppressed in %s: %s", meeting_id, reason)

    def stats(self, meeting_id: str) -> dict[str, int]:
        state = self._state(meeting_id)
        return {"posted": state.posted, "suppressed": state.suppressed}

    def clear(self, meeting_id: str) -> None:
        self._states.pop(meeting_id, None)

    def reset(self) -> None:
        self._states.clear()
