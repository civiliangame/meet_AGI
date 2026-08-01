"""The loop that runs after every person finishes speaking.

One entry point — `handle_final_segment` — called once per finalized utterance, from
the fixture harness today and from Recall's transcript stream when that lands. Partials
never reach here.

    ┌─ every finalized utterance
    │
    ├─ remember it (conversation context for both loops)
    │
    ├─ wake word?  ── yes ──▶ SPEECH MODE
    │                          retrieve → answer → speak → type into chat
    │
    └───────────── no ──────▶ AMBIENT MODE
                               triage → retrieve → look for a conflict → gate → type

Work is dispatched as a task per utterance and serialized behind a per-meeting lock, so
reasoning never blocks transcript ingestion and two interjections can never interleave.

Nothing in here raises into its caller. A meeting that keeps running with a degraded
copilot is strictly better than one where an exception in the reasoning path stops the
transcript.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from ..bus import bus
from ..chat import chat_router
from ..schemas import (
    AgentState,
    AgentStateChangedData,
    AnsweredData,
    Autonomy,
    Interjection,
    InterjectionKind,
    InterjectionStatus,
    InterjectionTrigger,
    QuestionCapturedData,
    TranscriptSegment,
    WakeDetectedData,
)
from ..store import store
from . import reason
from .context import ConversationMemory, Turn
from .gate import InterjectionGate
from .triage import is_checkable_claim
from .wake import WakeDetector, build_detector

log = logging.getLogger(__name__)

WAKE_DEBOUNCE_SECONDS = 3.0
"""Ignore a second wake word this soon after the last. DESIGN.md §5."""

QUESTION_CAPTURE_SECONDS = 20.0
"""How long to wait for the question after a bare wake word. DESIGN.md §5 step 2."""

ACK_CLIP_ID = "chime"
"""Played on wake, before Kindred has an answer. Confirms it is listening."""

_KIND_BY_VERDICT = {
    "contradiction": InterjectionKind.CONTRADICTION,
    "correction": InterjectionKind.CORRECTION,
    "context": InterjectionKind.CONTEXT,
}


class _PendingQuestion:
    """A wake word arrived without a question. Waiting for the next thing they say."""

    __slots__ = ("participant_id", "deadline", "segment_id")

    def __init__(self, participant_id: str, segment_id: str, deadline: float) -> None:
        self.participant_id = participant_id
        self.segment_id = segment_id
        self.deadline = deadline


class PipelineEngine:
    """Routes each finalized utterance into speech mode or the ambient loop."""

    def __init__(self) -> None:
        self.memory = ConversationMemory()
        self.gate = InterjectionGate()
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._last_wake: dict[str, float] = {}
        self._pending: dict[str, _PendingQuestion] = {}
        self._detector: WakeDetector | None = None
        self._detector_key: tuple[str, tuple[str, ...]] | None = None

    # --- entry point ---------------------------------------------------------------

    def handle_final_segment(self, segment: TranscriptSegment) -> None:
        """Feed one finalized utterance into the loop. Returns immediately."""
        turn = Turn(
            speaker=segment.speaker_name,
            text=segment.text,
            segment_id=segment.id,
            person_id=segment.person_id,
        )
        self.memory.add(segment.meeting_id, turn)

        task = asyncio.create_task(
            self._process(segment), name=f"pipeline:{segment.meeting_id}:{segment.id}"
        )
        # Hold a reference: asyncio only keeps weak ones, and a garbage-collected task
        # cancels mid-reasoning with no error anywhere.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(self, segment: TranscriptSegment) -> None:
        lock = self._locks.setdefault(segment.meeting_id, asyncio.Lock())
        async with lock:
            try:
                await self._route(segment)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pipeline failed on segment %s", segment.id)

    async def _route(self, segment: TranscriptSegment) -> None:
        meeting = store.meetings.get(segment.meeting_id)
        if meeting is None or meeting.agent_state == AgentState.MUTED:
            return

        settings = store.settings

        # A wake word landed last turn with no question attached. This utterance from the
        # same speaker is the question — unless they took too long, in which case the
        # capture window has closed and this is just ordinary talk again.
        pending = self._pending.get(segment.meeting_id)
        if pending is not None:
            if (
                pending.participant_id == segment.participant_id
                and time.monotonic() <= pending.deadline
            ):
                self._pending.pop(segment.meeting_id, None)
                await self._speech_mode(segment, question=segment.text)
                return
            if time.monotonic() > pending.deadline:
                self._pending.pop(segment.meeting_id, None)
                self._set_agent_state(
                    segment.meeting_id, AgentState.IDLE, "no question followed the wake word"
                )

        if settings.wake_word_enabled:
            match = self._detector_for(settings).match(segment.text)
            if match is not None and self._debounce_ok(segment.meeting_id):
                await self._on_wake(segment, match)
                return

        await self._ambient(segment)

    # --- speech mode ---------------------------------------------------------------

    async def _on_wake(self, segment: TranscriptSegment, match) -> None:
        self._last_wake[segment.meeting_id] = time.monotonic()
        bus.publish_meeting(
            segment.meeting_id,
            "speech.wake_detected",
            WakeDetectedData(
                participant_id=segment.participant_id,
                person_id=segment.person_id,
                segment_id=segment.id,
                matched_text=match.matched_text,
            ),
        )
        self._set_agent_state(segment.meeting_id, AgentState.LISTENING, "heard the wake word")

        if match.has_question:
            await self._speech_mode(segment, question=match.question)
            return

        # Woken with nothing to answer yet. Acknowledge out loud so the speaker knows to
        # keep going, and hold the floor for their next utterance.
        await self._say_clip(segment.meeting_id, ACK_CLIP_ID)
        self._pending[segment.meeting_id] = _PendingQuestion(
            participant_id=segment.participant_id,
            segment_id=segment.id,
            deadline=time.monotonic() + QUESTION_CAPTURE_SECONDS,
        )
        log.info("woken in %s; waiting for the question", segment.meeting_id)

    async def _speech_mode(self, segment: TranscriptSegment, *, question: str) -> None:
        meeting_id = segment.meeting_id
        question = question.strip()
        if not question:
            self._set_agent_state(meeting_id, AgentState.IDLE)
            return

        bus.publish_meeting(
            meeting_id,
            "speech.question_captured",
            QuestionCapturedData(question=question, segment_ids=[segment.id]),
        )
        self._set_agent_state(meeting_id, AgentState.THINKING, "searching the documents")

        transcript = self.memory.for_meeting(meeting_id).render(exclude_segment_id=segment.id)
        answer = await reason.answer_question(
            question=question, asker=segment.speaker_name, transcript=transcript
        )

        if answer is None:
            # No reasoning available (no API key) or the call failed. Say so out loud
            # rather than leaving the speaker waiting on silence.
            await self._say_clip(meeting_id, "no_context")
            self._set_agent_state(meeting_id, AgentState.IDLE)
            return

        interjection = store.build_interjection(
            meeting_id,
            kind=InterjectionKind.ANSWER,
            status=InterjectionStatus.POSTED,
            chat_alert=answer.chat_alert,
            headline=answer.headline,
            body_md=answer.body_md,
            confidence=answer.confidence,
            trigger=InterjectionTrigger(
                segment_ids=[segment.id], person_id=segment.person_id, quote=question
            ),
            citations=answer.citations,
            spoken=True,
        )
        self._record(meeting_id, interjection)

        # Speak first, type second. The room is waiting on audio; chat can lag.
        await self._say_text(meeting_id, answer.spoken)
        if store.settings.autonomy != Autonomy.SILENT:
            await chat_router.post(meeting_id, answer.chat_alert)

        bus.publish_meeting(
            meeting_id, "speech.answered", AnsweredData(interjection_id=interjection.id)
        )

    # --- ambient loop ---------------------------------------------------------------

    async def _ambient(self, segment: TranscriptSegment) -> None:
        meeting_id = segment.meeting_id

        checkable, _ = await is_checkable_claim(segment.text)
        if not checkable:
            return

        transcript = self.memory.for_meeting(meeting_id).render(exclude_segment_id=segment.id)
        verdict = await reason.check_claim(
            claim=segment.text, speaker=segment.speaker_name, transcript=transcript
        )
        if not verdict.is_flag:
            return

        decision = self.gate.check(meeting_id, verdict.confidence)
        if not decision:
            self.gate.record_suppressed(meeting_id, decision.reason)
            return

        autonomy = store.settings.autonomy
        posting = autonomy == Autonomy.AUTO_POST
        interjection = store.build_interjection(
            meeting_id,
            kind=_KIND_BY_VERDICT.get(verdict.kind, InterjectionKind.CONTEXT),
            status=InterjectionStatus.POSTED if posting else InterjectionStatus.PROPOSED,
            chat_alert=verdict.chat_alert,
            headline=verdict.headline,
            body_md=verdict.body_md,
            confidence=verdict.confidence,
            trigger=InterjectionTrigger(
                segment_ids=[segment.id],
                person_id=segment.person_id,
                quote=segment.text,
            ),
            citations=verdict.citations,
            spoken=False,
        )
        self._record(meeting_id, interjection)
        self.gate.record(meeting_id)

        if posting:
            await chat_router.post(meeting_id, verdict.chat_alert)

    # --- shared helpers --------------------------------------------------------------

    def _record(self, meeting_id: str, interjection: Interjection) -> None:
        store.interjections_for(meeting_id).append(interjection)
        meeting = store.meetings.get(meeting_id)
        if meeting is not None:
            meeting.stats.interjection_count += 1
        bus.publish_meeting(meeting_id, "interjection.proposed", interjection)

    async def _say_text(self, meeting_id: str, text: str) -> None:
        await self._say(meeting_id, text=text)

    async def _say_clip(self, meeting_id: str, clip_id: str) -> None:
        await self._say(meeting_id, clip_id=clip_id)

    async def _say(
        self, meeting_id: str, *, text: str | None = None, clip_id: str | None = None
    ) -> None:
        """Speak, tolerating a meeting with no audio channel.

        A harness replay has no bot, so it gets a null sink on first use rather than
        having speech disabled — the state transitions, timing, and events are then
        identical to a real meeting, which is the whole point of the harness.
        """
        from ..runtime import get_runtime

        try:
            runtime = get_runtime()
            if not runtime.speech.is_attached(meeting_id):
                runtime.attach_dry_run(meeting_id, label=meeting_id)
            await runtime.speech.say(meeting_id, text, clip_id=clip_id)
        except Exception:
            log.exception("could not speak in %s", meeting_id)
            self._set_agent_state(meeting_id, AgentState.IDLE)

    def _set_agent_state(
        self, meeting_id: str, state: AgentState, detail: str | None = None
    ) -> None:
        meeting = store.meetings.get(meeting_id)
        if meeting is not None:
            if meeting.agent_state == AgentState.MUTED:
                return
            meeting.agent_state = state
        bus.publish_meeting(
            meeting_id,
            "agent.state_changed",
            AgentStateChangedData(agent_state=state, detail=detail),
        )

    def _debounce_ok(self, meeting_id: str) -> bool:
        last = self._last_wake.get(meeting_id)
        return last is None or (time.monotonic() - last) >= WAKE_DEBOUNCE_SECONDS

    def _detector_for(self, settings) -> WakeDetector:
        """Cached detector, rebuilt when the wake word changes in settings."""
        key = (settings.wake_word, tuple(settings.wake_aliases))
        if self._detector is None or self._detector_key != key:
            self._detector = build_detector()
            self._detector_key = key
            log.info("wake phrases: %s", ", ".join(self._detector.phrases[:8]))
        return self._detector

    # --- lifecycle -------------------------------------------------------------------

    def forget(self, meeting_id: str) -> None:
        self.memory.clear(meeting_id)
        self.gate.clear(meeting_id)
        self._locks.pop(meeting_id, None)
        self._last_wake.pop(meeting_id, None)
        self._pending.pop(meeting_id, None)
        chat_router.detach(meeting_id)

    def reset(self) -> None:
        self.memory.reset()
        self.gate.reset()
        self._locks.clear()
        self._last_wake.clear()
        self._pending.clear()
        self._detector = None
        self._detector_key = None

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait for in-flight reasoning to finish. Used on shutdown and in tests."""
        if not self._tasks:
            return
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*list(self._tasks), return_exceptions=True), timeout=timeout
            )


engine = PipelineEngine()


def handle_final_segment(segment: TranscriptSegment) -> None:
    """Run the loop for one finalized utterance. The pipeline's only entry point."""
    engine.handle_final_segment(segment)
