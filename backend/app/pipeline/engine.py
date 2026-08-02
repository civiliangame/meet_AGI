"""The loop that runs after every person finishes speaking.

One entry point — `handle_final_segment` — called once per finalized utterance, from
the fixture harness today and from Recall's transcript stream when that lands. Partials
never reach here.

    ┌─ every finalized utterance
    │
    ├─ remember it (conversation context for both loops)
    │
    ├─ kill phrase? ─ yes ──▶ STOP. Cancel the audio, drop the queue, forget the
    │                          question, kill the reasoning that was in flight.
    │
    ├─ wake word?  ── yes ──▶ SPEECH MODE
    │                          retrieve → answer → speak → type into chat
    │
    └───────────── no ──────▶ AMBIENT MODE
                               scan → retrieve → look for a contradiction → gate → type

The ambient half re-reads the whole recent window every time rather than judging the
newest sentence against history. That is what makes it work in a conference room: when
several people share one microphone the platform labels them all the same, several turns
land inside a single buffered utterance, and both halves of a contradiction are
routinely already "earlier". Nothing downstream may reason from speaker identity.

Work is dispatched as a task per utterance and serialized behind a per-meeting lock, so
reasoning never blocks transcript ingestion and two interjections can never interleave.

The kill phrase is the one thing that does not queue behind that lock. `handle_stop`
runs immediately, on the ingest task, because everything it does is a cancellation —
waiting for the lock would mean waiting for the very work it exists to abort.

Nothing in here raises into its caller. A meeting that keeps running with a degraded
copilot is strictly better than one where an exception in the reasoning path stops the
transcript.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time

from ..audio import AudioClip
from ..bus import bus
from ..chat import chat_router
from ..chat.sinks import with_trigger_prefix
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
    SpeechInterruptedData,
    TranscriptSegment,
    WakeDetectedData,
)
from ..schemas.speech import Utterance
from ..speech.fillers import SAMPLE_FILLER_CLIP_ID as FILLER_FALLBACK_CLIP_ID
from ..store import store
from ..video import card
from . import reason
from .context import ConversationMemory, Turn
from .gate import InterjectionGate
from .triage import scan_for_conflict
from .wake import StopDetector, WakeDetector, build_detector, build_stop_detector

log = logging.getLogger(__name__)

_WORD = re.compile(r"\w+")
"""Tokenizer for the contradiction fingerprint. See `_fingerprint`."""

WAKE_DEBOUNCE_SECONDS = 3.0
"""Ignore a second wake word this soon after the last. DESIGN.md §5."""

STOP_DEBOUNCE_SECONDS = 1.5
"""Ignore a repeat kill phrase this soon after the last.

Short on purpose. The kill phrase arrives on partial transcript, so the same sentence
fires it several times as the words land — but someone who says "stop" twice because the
first one did not seem to take must not be ignored.
"""

QUESTION_CAPTURE_SECONDS = 20.0
"""How long to wait for the question after a bare wake word. DESIGN.md §5 step 2."""

ACK_CLIP_ID = "chime"
"""Played on wake, before Meet AGI has an answer. Confirms it is listening."""


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
        self._by_meeting: dict[str, set[asyncio.Task[None]]] = {}
        """The same tasks, indexed so the kill phrase can cancel one meeting's work."""
        self._last_wake: dict[str, float] = {}
        self._last_stop: dict[str, float] = {}
        self._pending: dict[str, _PendingQuestion] = {}
        self._flagged: dict[str, set[str]] = {}
        """Contradictions already announced, so a re-scanned window stays quiet."""
        self._detector: WakeDetector | None = None
        self._detector_key: tuple[str, tuple[str, ...]] | None = None
        self._stop_detector: StopDetector | None = None
        self._stop_detector_key: tuple[str, tuple[str, ...]] | None = None

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
        self._by_meeting.setdefault(segment.meeting_id, set()).add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._forget_task(segment.meeting_id))

    def _forget_task(self, meeting_id: str):
        def done(task: asyncio.Task[None]) -> None:
            tasks = self._by_meeting.get(meeting_id)
            if tasks is not None:
                tasks.discard(task)
                if not tasks:
                    self._by_meeting.pop(meeting_id, None)

        return done

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

        # Normally the kill phrase has already fired from partial transcript and this is
        # the finalized copy of the same sentence arriving late. Either way it stops
        # here: "AGI, stop talking" is not a question and it is not a checkable claim.
        if self.stop_match(segment.text) is not None:
            await self.handle_stop(segment.meeting_id, source=segment.speaker_name)
            return

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

    # --- the kill phrase --------------------------------------------------------------

    def stop_match(self, text: str):
        """Whether this text tells Meet AGI to shut up. Safe to call on partials."""
        return self._stop_detector_for(store.settings).match(text)

    def sees_wake(self, text: str) -> bool:
        """Whether this text holds a wake phrase.

        The ingest buffer uses this to decide how long to keep waiting for the rest of
        the sentence — not to wake anything. Waking still happens only on a finalized
        utterance.
        """
        settings = store.settings
        if not settings.wake_word_enabled:
            return False
        return self._detector_for(settings).match(text) is not None

    async def handle_stop(
        self, meeting_id: str, *, source: str = "someone", force: bool = False
    ) -> list[Utterance]:
        """Cut Meet AGI off. Returns the utterances that were discarded.

        Called straight from ingest the moment the phrase is heard, ahead of the
        per-meeting lock, because every step is an abort:

        1. cancel the reasoning already in flight, so an answer that is half generated
           never arrives thirty seconds later to a room that asked for silence;
        2. drop the pending-question window, so the next thing anyone says is not
           mistaken for the question Meet AGI was still waiting for;
        3. cut the audio — queue and current clip both.

        Order matters. Cancelling reasoning first means nothing can enqueue new speech
        behind the drain.

        `force` skips the debounce. The debounce exists because the kill phrase arrives
        repeatedly as partial transcript revises; a human clicking the button in the
        dashboard is not that, and being told "you already stopped it" when it is still
        talking would be maddening.
        """
        if not force and not self._stop_debounce_ok(meeting_id):
            return []
        self._last_stop[meeting_id] = time.monotonic()

        log.info("STOP in %s — %s told Meet AGI to stop talking", meeting_id, source)

        cancelled = 0
        for task in list(self._by_meeting.get(meeting_id, ())):
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
                cancelled += 1

        self._pending.pop(meeting_id, None)

        dropped: list[Utterance] = []
        try:
            from ..runtime import get_runtime

            runtime = get_runtime()
            if runtime.speech.is_attached(meeting_id):
                dropped = await runtime.speech.stop(meeting_id)
        except Exception:
            log.exception("could not stop audio in %s", meeting_id)

        self._set_agent_state(meeting_id, AgentState.IDLE, "stopped")
        bus.publish_meeting(
            meeting_id,
            "speech.interrupted",
            SpeechInterruptedData(
                source=source,
                dropped_utterances=len(dropped),
                cancelled_tasks=cancelled,
            ),
        )
        return dropped

    # --- speech mode ---------------------------------------------------------------

    async def _on_wake(self, segment: TranscriptSegment, match) -> None:
        self._last_wake[segment.meeting_id] = time.monotonic()
        # Logged as well as published: when the wake word misfires on stage, the server
        # log is what you actually have in front of you, and knowing which variant
        # matched is the difference between a fix and a guess.
        log.info(
            "WAKE in %s — matched %r, question=%r",
            segment.meeting_id,
            match.matched_text,
            match.question[:80],
        )
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

        # Say something *before* reasoning, not after. Retrieval plus generation is a
        # couple of seconds, and to the person who just asked out loud, silence reads as
        # "it didn't hear me". The filler is pre-rendered, so queueing it costs nothing,
        # and because playback is serialized per meeting the real answer automatically
        # waits for it to finish rather than talking over it.
        await self.say_filler(meeting_id)

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

        # The prefix is stored on the interjection, not added at post time, so the
        # dashboard shows byte-for-byte what the meeting saw.
        chat_alert = with_trigger_prefix(answer.chat_alert, answer.topic)
        interjection = store.build_interjection(
            meeting_id,
            kind=InterjectionKind.ANSWER,
            status=InterjectionStatus.POSTED,
            chat_alert=chat_alert,
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
            await chat_router.post(meeting_id, chat_alert)

        bus.publish_meeting(
            meeting_id, "speech.answered", AnsweredData(interjection_id=interjection.id)
        )

    # --- ambient loop ---------------------------------------------------------------

    async def _ambient(self, segment: TranscriptSegment) -> None:
        """The unprompted half. Contradictions only — nothing else earns an interjection.

        `Verdict.is_flag` is where that rule is enforced: it holds only when the model
        returned "contradiction" *and* could quote both of the statements that cannot
        both be true. A verdict without that pair is a model narrating rather than
        catching something, and the room does not need it.
        """
        meeting_id = segment.meeting_id
        quote = segment.text[:70]

        # Every branch below logs. The ambient loop used to be completely silent unless
        # it fired, so "it never comments" and "it comments but the chat sink is broken"
        # and "the model says none every time" all looked identical from the outside —
        # which is exactly the position this feature was debugged from. One line per
        # utterance is cheap next to the model call it is reporting on.
        scan = await scan_for_conflict(
            transcript=self.memory.for_meeting(meeting_id).render(), latest=segment.text
        )
        if not scan:
            log.info("[%s] ambient skip (%s): %r", meeting_id, scan.reason, quote)
            return

        # The *whole* window, latest line included. The model is hunting a conflicting
        # pair anywhere in the exchange, and excluding the newest line would hide the
        # commonest case in a shared conference room: two people arguing inside a single
        # buffered utterance.
        transcript = self.memory.for_meeting(meeting_id).render()
        verdict = await reason.check_claim(
            claim=segment.text, speaker=segment.speaker_name, transcript=transcript
        )
        if not verdict.is_flag:
            log.info("[%s] ambient: no conflict in %r", meeting_id, quote)
            return

        log.info(
            "[%s] CONTRADICTION (%.2f) — %r vs %r",
            meeting_id,
            verdict.confidence,
            verdict.statement_a[:60],
            verdict.statement_b[:60],
        )

        # Now that the model re-reads the whole window on every utterance, it finds the
        # same conflict again on the next line, and the next. The cooldown alone would
        # turn that into "one interjection, then twenty suppressed" and the *next*,
        # genuinely new contradiction would land in the middle of that noise. Fingerprint
        # the pair instead, so a conflict is announced once and a new one is never
        # rationed on account of an old one.
        fingerprint = self._fingerprint(verdict)
        seen = self._flagged.setdefault(meeting_id, set())
        if fingerprint in seen:
            log.info("[%s] already flagged this pair; staying quiet", meeting_id)
            return
        seen.add(fingerprint)

        decision = self.gate.check(meeting_id, verdict.confidence)
        if not decision:
            # Logged loudly. A real contradiction that the room never heard because of a
            # cooldown is a product decision, and it should be visible as one.
            log.warning("[%s] contradiction suppressed: %s", meeting_id, decision.reason)
            self.gate.record_suppressed(meeting_id, decision.reason)
            return

        autonomy = store.settings.autonomy
        posting = autonomy == Autonomy.AUTO_POST
        interjection = store.build_interjection(
            meeting_id,
            kind=InterjectionKind.CONTRADICTION,
            status=InterjectionStatus.POSTED if posting else InterjectionStatus.PROPOSED,
            chat_alert=with_trigger_prefix(verdict.chat_alert, verdict.topic),
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
            await chat_router.post(meeting_id, interjection.chat_alert)
        else:
            # The other reason "it never comments" looks like a detection failure when it
            # is not: the contradiction was found and the autonomy level held it back.
            log.warning(
                "[%s] contradiction held back — autonomy is %s, so it reached the "
                "dashboard but not the meeting",
                meeting_id,
                autonomy.value,
            )

    # --- shared helpers --------------------------------------------------------------

    @staticmethod
    def _fingerprint(verdict) -> str:
        """Identify a conflicting pair, so the same one is not announced twice.

        Order-insensitive and loosely normalized: the model re-quotes the same two
        statements slightly differently from one turn to the next — trailing
        punctuation, a clipped opening word — and an exact-string key would treat each
        rewording as a fresh contradiction.
        """
        def key(text: str) -> str:
            return " ".join(_WORD.findall(text.casefold()))[:160]

        return "|".join(sorted((key(verdict.statement_a), key(verdict.statement_b))))

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

    async def say_filler(self, meeting_id: str) -> None:
        """Queue a cached "let me look that up" line, in Meet AGI's own voice.

        Falls back to the sample provider's `checking` clip, which says much the same
        thing — so this works whether or not real TTS is configured, and a filler that
        cannot be produced is never allowed to hold up the answer behind it.
        """
        from ..runtime import get_runtime

        try:
            clip = await get_runtime().fillers.next_clip()
        except Exception:
            log.exception("filler synthesis failed in %s", meeting_id)
            clip = None

        if clip is None:
            await self._say_clip(meeting_id, FILLER_FALLBACK_CLIP_ID)
        else:
            await self._say(meeting_id, clip=clip)

    async def _say(
        self,
        meeting_id: str,
        *,
        text: str | None = None,
        clip_id: str | None = None,
        clip: AudioClip | None = None,
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
            await runtime.speech.say(meeting_id, text, clip_id=clip_id, clip=clip)
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

    def _stop_debounce_ok(self, meeting_id: str) -> bool:
        last = self._last_stop.get(meeting_id)
        return last is None or (time.monotonic() - last) >= STOP_DEBOUNCE_SECONDS

    def _detector_for(self, settings) -> WakeDetector:
        """Cached detector, rebuilt when the wake word changes in settings."""
        key = (settings.wake_word, tuple(settings.wake_aliases))
        if self._detector is None or self._detector_key != key:
            self._detector = build_detector()
            self._detector_key = key
            log.info("wake phrases: %s", ", ".join(self._detector.phrases[:8]))
        return self._detector

    def _stop_detector_for(self, settings) -> StopDetector:
        """Cached kill-phrase detector, rebuilt when the wake word changes."""
        key = (settings.wake_word, tuple(settings.wake_aliases))
        if self._stop_detector is None or self._stop_detector_key != key:
            self._stop_detector = build_stop_detector()
            self._stop_detector_key = key
            log.info("stop phrase names: %s", ", ".join(self._stop_detector.names[:8]))
        return self._stop_detector

    # --- lifecycle -------------------------------------------------------------------

    def forget(self, meeting_id: str) -> None:
        self.memory.clear(meeting_id)
        self.gate.clear(meeting_id)
        self._locks.pop(meeting_id, None)
        self._last_wake.pop(meeting_id, None)
        self._last_stop.pop(meeting_id, None)
        self._pending.pop(meeting_id, None)
        self._flagged.pop(meeting_id, None)
        self._by_meeting.pop(meeting_id, None)
        chat_router.detach(meeting_id)
        card.forget(meeting_id)

    def reset(self) -> None:
        self.memory.reset()
        self.gate.reset()
        self._locks.clear()
        self._last_wake.clear()
        self._last_stop.clear()
        self._pending.clear()
        self._flagged.clear()
        self._by_meeting.clear()
        self._detector = None
        self._detector_key = None
        self._stop_detector = None
        self._stop_detector_key = None

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
    """Run the loop for one finalized utterance. The pipeline's main entry point."""
    engine.handle_final_segment(segment)


async def handle_stop(
    meeting_id: str, *, source: str = "someone", force: bool = False
) -> list[Utterance]:
    """Cut Meet AGI off mid-sentence. Called from ingest on the kill phrase."""
    return await engine.handle_stop(meeting_id, source=source, force=force)
