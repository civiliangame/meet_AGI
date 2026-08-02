"""Recall real-time events → the reasoning pipeline.

This is the other half of `ingest/`: the harness fabricates finalized utterances, and
this turns real ones into the identical shape. Everything downstream — wake detection,
retrieval, the ambient loop — cannot tell which produced a segment.

**The hard part is that Recall does not send utterances, it sends words.** A
`transcript.data` event is documented as a finalized utterance, but Recall's own FAQ
notes the provider frequently streams word-by-word to minimize latency. Feeding those
straight into wake detection would mean matching "Hey AGI" against the single word
"hey", so this module buffers words per speaker and decides when a *person* stopped
talking:

- terminal punctuation (`.`, `?`, `!`) flushes immediately — the provider has told us
  the sentence is over, and waiting longer only adds latency to the wake response;
- otherwise a short silence gap flushes, which is the literal reading of "after every
  person finishes speaking";
- **and a hard ceiling flushes regardless.** This one is not an optimization. A silence
  gap only ever arrives if there is silence, and in a real meeting with an open mic
  there is not: breathing, keyboards, someone else's dog, and the next sentence all
  land inside the gap and reset it. The buffer then grows without ever flushing and
  Kindred answers nothing at all until somebody mutes — which is precisely the bug this
  ceiling exists to kill. Once a wake phrase is in the buffer both windows tighten,
  because at that point somebody is visibly waiting for an answer.

Partials go to the frontend as they arrive so the live transcript line still moves,
and only the flushed utterance reaches the reasoning pipeline. That is DESIGN.md §5's
rule about never waking on a partial, enforced at the ingest boundary rather than
trusted to every consumer.

**The kill phrase is the deliberate exception.** "AGI, stop talking" is checked against
partials, before buffering, on every event. Acting a second early on a phrase whose only
effect is silence is the right trade; acting a second late means Kindred talks over the
person telling it to stop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..bus import bus
from ..ids import PREFIX_SEGMENT, new_id
from ..schemas import (
    ParticipantJoinedData,
    ParticipantLeftData,
    ParticipantSpeakingChangedData,
    RosterEntry,
    TranscriptSegment,
)
from ..store import store

log = logging.getLogger(__name__)

TERMINAL_PUNCTUATION = (".", "?", "!")

# Words arrive tokenized, so naive " ".join puts a space before every comma.
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?%])")
_SPACE_INSIDE_QUOTE = re.compile(r"\s+'")


def join_words(words: list[dict[str, Any]]) -> str:
    """Reassemble Recall's word array into readable text."""
    text = " ".join(str(w.get("text", "")).strip() for w in words if w.get("text"))
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    return _SPACE_INSIDE_QUOTE.sub("'", text).strip()


@dataclass
class _Buffer:
    """Words accumulated for one speaker since their last flush."""

    participant_id: str
    speaker_name: str
    words: list[dict[str, Any]] = field(default_factory=list)
    segment_id: str = field(default_factory=lambda: new_id(PREFIX_SEGMENT))
    start_ms: int | None = None
    end_ms: int = 0
    flusher: asyncio.Task[None] | None = None
    ceiling: asyncio.Task[None] | None = None
    """Fires once, on a wall clock, whatever the words are doing. See the module note."""
    opened_at: float = 0.0
    woken_at: float | None = None
    """When a wake phrase first appeared in this buffer, if it has."""

    @property
    def text(self) -> str:
        return join_words(self.words)


def _ms(timestamp: Any) -> int | None:
    """`{"relative": 12.34}` → milliseconds from recording start."""
    if isinstance(timestamp, dict) and isinstance(timestamp.get("relative"), (int, float)):
        return int(timestamp["relative"] * 1000)
    return None


class RecallLiveIngest:
    """Consumes Recall real-time events for every live meeting."""

    def __init__(
        self,
        *,
        silence_ms: int = 1000,
        wake_silence_ms: int = 400,
        max_utterance_ms: int = 9000,
        wake_max_ms: int = 4500,
    ) -> None:
        self._silence = silence_ms / 1000
        self._wake_silence = wake_silence_ms / 1000
        self._max_utterance = max_utterance_ms / 1000
        self._wake_max = wake_max_ms / 1000
        self._buffers: dict[tuple[str, str], _Buffer] = {}

    # --- event routing ---------------------------------------------------------------

    async def handle(self, payload: dict[str, Any]) -> str:
        """Dispatch one real-time event. Returns the event name it handled."""
        event = str(payload.get("event", ""))
        data = payload.get("data") or {}

        meeting_id = self._meeting_id_for(data)
        if meeting_id is None:
            log.warning("real-time event %s for an unknown bot; ignoring", event)
            return event

        if event == "transcript.data":
            await self._on_words(meeting_id, data.get("data") or {}, final=True)
        elif event == "transcript.partial_data":
            await self._on_words(meeting_id, data.get("data") or {}, final=False)
        elif event.startswith("participant_events."):
            self._on_participant(meeting_id, event.split(".", 1)[1], data.get("data") or {})
        return event

    def _meeting_id_for(self, data: dict[str, Any]) -> str | None:
        """Map a Recall event back to our meeting.

        `metadata.meeting_id` is set at bot creation, so it is the direct route. The
        bot-id scan is the fallback for a bot dispatched before that was wired, or one
        restarted out of band.
        """
        bot = data.get("bot") or {}
        if isinstance(bot.get("metadata"), dict):
            if meeting_id := bot["metadata"].get("meeting_id"):
                if meeting_id in store.meetings:
                    return str(meeting_id)

        bot_id = bot.get("id")
        if bot_id:
            for meeting in store.meetings.values():
                if meeting.bot_id == bot_id:
                    return meeting.id
        return None

    # --- transcript ------------------------------------------------------------------

    async def _on_words(self, meeting_id: str, part: dict[str, Any], *, final: bool) -> None:
        words = part.get("words") or []
        if not words:
            return

        participant = part.get("participant") or {}
        participant_id = str(participant.get("id") or "unknown")
        speaker_name = participant.get("name") or f"Participant {participant_id}"

        key = (meeting_id, participant_id)
        buffer = self._buffers.get(key)
        if buffer is None:
            buffer = _Buffer(
                participant_id=participant_id,
                speaker_name=speaker_name,
                opened_at=time.monotonic(),
            )
            self._buffers[key] = buffer
        buffer.speaker_name = speaker_name or buffer.speaker_name

        incoming = join_words(words)

        # Kill phrase first, and against the buffer *plus* what just arrived, so
        # "AGI," landing in one event and "stop talking" in the next still fires. This
        # runs on partials too — the whole point is not to wait for the sentence.
        if await self._check_stop(meeting_id, key, buffer, incoming):
            return

        if not final:
            # Partials are for the frontend's live line only. They are never buffered,
            # because a revised partial would double up words in the final utterance.
            self._publish(meeting_id, buffer, incoming, is_final=False)
            return

        buffer.words.extend(words)
        if buffer.start_ms is None:
            buffer.start_ms = _ms(words[0].get("start_timestamp")) or 0
        buffer.end_ms = _ms(words[-1].get("end_timestamp")) or buffer.end_ms

        text = buffer.text
        self._publish(meeting_id, buffer, text, is_final=False)

        if buffer.woken_at is None and self._sees_wake(text):
            buffer.woken_at = time.monotonic()
            log.info(
                "[%s] wake phrase in progress from %s; tightening the flush window",
                meeting_id,
                buffer.speaker_name,
            )

        self._cancel_silence(buffer)
        if text.endswith(TERMINAL_PUNCTUATION):
            await self._flush(meeting_id, key)
            return

        # Only the silence timer was cancelled above. The ceiling deliberately survives
        # incoming words — restarting it on every word is what the silence timer already
        # does, and it is exactly the behaviour that lets an open mic postpone the flush
        # forever.
        self._arm_ceiling(meeting_id, key, buffer)
        buffer.flusher = asyncio.create_task(
            self._flush_after_silence(meeting_id, key, buffer), name=f"flush:{meeting_id}"
        )

    async def _check_stop(
        self, meeting_id: str, key: tuple[str, str], buffer: _Buffer, incoming: str
    ) -> bool:
        """Fire the kill phrase if it is in this speaker's stream. Returns whether it was.

        On a hit the buffer is thrown away rather than flushed: "AGI, stop talking" is
        not a question to answer and not a claim to check, and letting it through as an
        ordinary utterance would have the pipeline reasoning about the very sentence
        that just told it to stop.
        """
        from ..pipeline.engine import engine

        candidate = f"{buffer.text} {incoming}".strip()
        if not candidate:
            return False
        try:
            match = engine.stop_match(candidate)
        except Exception:
            log.exception("stop-phrase matching failed")
            return False
        if match is None:
            return False

        self._drop(key)
        fired = await engine.handle_stop(meeting_id, source=buffer.speaker_name)
        if fired:
            log.info(
                "[%s] kill phrase from %s: %r",
                meeting_id,
                buffer.speaker_name,
                match.matched_text,
            )
        return True

    @staticmethod
    def _sees_wake(text: str) -> bool:
        from ..pipeline.engine import engine

        try:
            return engine.sees_wake(text)
        except Exception:
            log.exception("wake pre-scan failed")
            return False

    def _arm_ceiling(self, meeting_id: str, key: tuple[str, str], buffer: _Buffer) -> None:
        """Guarantee this buffer flushes, whether or not the speaker ever pauses."""
        if buffer.ceiling is not None and not buffer.ceiling.done():
            return
        buffer.ceiling = asyncio.create_task(
            self._flush_at_ceiling(meeting_id, key, buffer), name=f"ceiling:{meeting_id}"
        )

    def _deadline(self, buffer: _Buffer) -> float:
        """Seconds from now until this buffer must flush no matter what."""
        latest = buffer.opened_at + self._max_utterance
        if buffer.woken_at is not None:
            latest = min(latest, buffer.woken_at + self._wake_max)
        return max(latest - time.monotonic(), 0.0)

    async def _flush_at_ceiling(
        self, meeting_id: str, key: tuple[str, str], buffer: _Buffer
    ) -> None:
        try:
            # Re-checked in a loop rather than slept once, because a wake phrase heard
            # after the ceiling was armed shortens the deadline it is waiting on.
            while (remaining := self._deadline(buffer)) > 0:
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return
        if self._buffers.get(key) is not buffer:
            return
        log.info(
            "[%s] flushing %s at the ceiling — still talking, no gap to wait for",
            meeting_id,
            buffer.speaker_name,
        )
        await self._flush(meeting_id, key)

    async def _flush_after_silence(
        self, meeting_id: str, key: tuple[str, str], buffer: _Buffer
    ) -> None:
        # The wake path only ever shortens the wait. A deployment that has tuned the
        # ordinary gap below the wake gap wants the tighter of the two, not the looser.
        gap = self._silence
        if buffer.woken_at is not None:
            gap = min(gap, self._wake_silence)
        try:
            await asyncio.sleep(gap)
        except asyncio.CancelledError:
            return
        await self._flush(meeting_id, key)

    def _drop(self, key: tuple[str, str]) -> None:
        """Discard a buffer and its timers without emitting anything."""
        buffer = self._buffers.pop(key, None)
        if buffer is not None:
            self._cancel_timers(buffer)

    async def _flush(self, meeting_id: str, key: tuple[str, str]) -> None:
        """Emit the buffered words as one finalized utterance and run the pipeline."""
        buffer = self._buffers.pop(key, None)
        if buffer is None:
            return
        self._cancel_timers(buffer)

        text = buffer.text
        if not text:
            return

        segment = self._segment(meeting_id, buffer, text, is_final=True)
        store.upsert_segment(segment)

        meeting = store.meetings.get(meeting_id)
        if meeting is not None:
            meeting.stats.utterance_count += 1

        bus.publish_meeting(meeting_id, "transcript.final", segment)
        log.info("[%s] %s: %s", meeting_id, buffer.speaker_name, text)

        # The same call the fixture harness makes. Returns immediately.
        from ..pipeline import handle_final_segment

        handle_final_segment(segment)

    @staticmethod
    def _cancel_silence(buffer: _Buffer) -> None:
        """Stop the gap timer. The ceiling is untouched — that is the whole point."""
        if buffer.flusher is not None and not buffer.flusher.done():
            buffer.flusher.cancel()
        buffer.flusher = None

    @staticmethod
    def _cancel_timers(buffer: _Buffer) -> None:
        """Stop both timers. The buffer is done with, one way or another.

        Skips the running task: `_flush_at_ceiling` calls `_flush`, which lands here,
        and a task that cancels itself never returns to log what it did.
        """
        for task in (buffer.flusher, buffer.ceiling):
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
        buffer.flusher = None
        buffer.ceiling = None

    def _publish(self, meeting_id: str, buffer: _Buffer, text: str, *, is_final: bool) -> None:
        if not text:
            return
        bus.publish_meeting(
            meeting_id,
            "transcript.final" if is_final else "transcript.partial",
            self._segment(meeting_id, buffer, text, is_final=is_final),
        )

    def _segment(
        self, meeting_id: str, buffer: _Buffer, text: str, *, is_final: bool
    ) -> TranscriptSegment:
        person = store.person_by_name(buffer.speaker_name)
        return TranscriptSegment(
            # Partials and their final share an id, so the frontend replaces the live
            # line rather than appending a duplicate.
            id=buffer.segment_id,
            meeting_id=meeting_id,
            participant_id=buffer.participant_id,
            person_id=person.id if person else None,
            speaker_name=person.display_name if person else buffer.speaker_name,
            text=text,
            is_final=is_final,
            start_ms=buffer.start_ms or 0,
            end_ms=buffer.end_ms,
            confidence=None,
        )

    # --- participants ------------------------------------------------------------------

    def _on_participant(self, meeting_id: str, action: str, data: dict[str, Any]) -> None:
        meeting = store.meetings.get(meeting_id)
        if meeting is None:
            return

        participant = data.get("participant") or data
        participant_id = str(participant.get("id") or "unknown")
        name = participant.get("name") or f"Participant {participant_id}"

        if action == "join":
            if any(e.participant_id == participant_id for e in meeting.roster):
                return
            person = store.person_by_name(name)
            entry = RosterEntry(
                participant_id=participant_id,
                person_id=person.id if person else None,
                display_name=person.display_name if person else name,
                is_host=bool(participant.get("is_host")),
                matched=person is not None,
                is_speaking=False,
            )
            meeting.roster.append(entry)
            meeting.stats.participant_count = len(meeting.roster)
            bus.publish_meeting(
                meeting_id, "participant.joined", ParticipantJoinedData(participant=entry)
            )

        elif action == "leave":
            meeting.roster = [e for e in meeting.roster if e.participant_id != participant_id]
            meeting.stats.participant_count = len(meeting.roster)
            bus.publish_meeting(
                meeting_id,
                "participant.left",
                ParticipantLeftData(participant_id=participant_id),
            )

        elif action in ("speech_on", "speech_off"):
            speaking = action == "speech_on"
            for entry in meeting.roster:
                if entry.participant_id == participant_id:
                    entry.is_speaking = speaking
                    break
            bus.publish_meeting(
                meeting_id,
                "participant.speaking_changed",
                ParticipantSpeakingChangedData(
                    participant_id=participant_id, is_speaking=speaking
                ),
            )

    # --- lifecycle -----------------------------------------------------------------------

    def forget(self, meeting_id: str) -> None:
        for key in [k for k in self._buffers if k[0] == meeting_id]:
            buffer = self._buffers.pop(key)
            self._cancel_timers(buffer)


ingest = RecallLiveIngest()


def configure(
    silence_ms: int,
    *,
    wake_silence_ms: int | None = None,
    max_utterance_ms: int | None = None,
    wake_max_ms: int | None = None,
) -> None:
    """Retune the utterance-boundary windows. Called once at startup from config."""
    ingest._silence = silence_ms / 1000
    if wake_silence_ms is not None:
        ingest._wake_silence = wake_silence_ms / 1000
    if max_utterance_ms is not None:
        ingest._max_utterance = max_utterance_ms / 1000
    if wake_max_ms is not None:
        ingest._wake_max = wake_max_ms / 1000
    log.info(
        "transcript windows: gap %dms (%dms after a wake), ceiling %.1fs (%.1fs after a wake)",
        silence_ms,
        ingest._wake_silence * 1000,
        ingest._max_utterance,
        ingest._wake_max,
    )
