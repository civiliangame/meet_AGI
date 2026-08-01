"""Fixture replay — a fake meeting that emits the real event stream.

This exists so the frontend can be built end to end with no live meeting, no Recall
API key, and no internet connection. Every frame it publishes is indistinguishable
from a real meeting to any consumer of the WebSocket contract; the only difference is
`Meeting.source == "harness"`.

It also gives you a deterministic, rehearsed demo path if conference wifi fails.

The fixture format is JSONL, one event per line, ordered by `at_ms`:

  {"kind":"meta","id":...,"title":...,"description":...,"duration_seconds":...}
  {"at_ms":0,"kind":"participant_join","participant_id":"p_1","display_name":"...","is_host":true}
  {"at_ms":3000,"kind":"utterance","participant_id":"p_1","duration_ms":5600,"text":"..."}
  {"at_ms":25000,"kind":"utterance",...,"triggers_interjection":"revenue_contradiction"}
  {"at_ms":61000,"kind":"wake",...,"question":"...","answer_ref":"new_product_line_answer"}
  {"at_ms":161000,"kind":"end"}

`utterance` is expanded into realistic partials followed by a final, plus
speaking-state changes, so the frontend's "live line" rendering has something to
exercise. Reasoning output is canned (see `_CANNED`) — Milestone 4 replaces it with
the real pipeline behind the same events.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..bus import bus
from ..ids import PREFIX_CHUNK, PREFIX_SEGMENT, new_id
from ..schemas import (
    AgentState,
    AgentStateChangedData,
    AnsweredData,
    Citation,
    Fixture,
    Interjection,
    InterjectionKind,
    InterjectionStatus,
    InterjectionTrigger,
    Meeting,
    MeetingSource,
    MeetingState,
    ParticipantJoinedData,
    ParticipantSpeakingChangedData,
    QuestionCapturedData,
    RosterEntry,
    TranscriptSegment,
    WakeDetectedData,
    utcnow,
)
from ..schemas.settings import Autonomy
from ..store import DECK_DOC_ID, NOTES_DOC_ID, store

log = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "meetings"

_PARTIAL_INTERVAL_MS = 700
"""How often a partial is emitted while someone is talking. Roughly matches what
Recall's real-time transcription produces."""


# --- Canned reasoning output -----------------------------------------------------
# Fixture content, not real reasoning. Milestone 4 generates these for real; the
# event shapes and the 500-char chat_alert discipline are already final.

_CANNED: dict[str, dict[str, Any]] = {
    "revenue_contradiction": {
        "kind": InterjectionKind.CONTRADICTION,
        "headline": "Marcus's new-product revenue claim conflicts with the Q3 board deck",
        "chat_alert": (
            "⚠️ Kindred: the new-product revenue claim conflicts with the Q3 deck "
            "— p.14 shows that line at -12.1% MoM, not +8%. Looks like a gross vs. net "
            "mismatch. Full analysis in the Kindred dashboard."
        ),
        "body_md": (
            "**Claim.** Marcus said new-product revenue is *up about eight percent* "
            "this quarter.\n\n"
            "**What the documents say.** The Q3 board deck (p.14) reports the new product "
            "line at **$1.42M, down 12.1% month-over-month**. That is the net revenue "
            "figure the deck carries into the board summary.\n\n"
            "**Most likely explanation.** Marcus is quoting **gross bookings**, which the "
            "product-line review notes track separately and which *did* rise in July on a "
            "few larger mid-market closes. Gross bookings and net revenue diverge here "
            "because of contract timing and a credit adjustment noted on p.16.\n\n"
            "**Why it matters.** These two numbers point in opposite directions and the "
            "board deck uses the net figure. Presenting +8% on Thursday against a deck "
            "that says -12.1% is the kind of discrepancy that costs credibility in the room."
        ),
        "confidence": 0.82,
        "citations": [
            {
                "document_id": DECK_DOC_ID,
                "filename": "Q3-board-deck.pdf",
                "chunk_id": f"{PREFIX_CHUNK}_01J8XK5B6C7D8E9F0G1H2J",
                "page": 14,
                "quote": "New Product Line: $1.42M (-12.1% MoM)",
                "relevance": 0.91,
            },
            {
                "document_id": NOTES_DOC_ID,
                "filename": "product-line-review-notes.md",
                "chunk_id": f"{PREFIX_CHUNK}_01J8XK5B6C7D8E9F0G1H3K",
                "page": None,
                "quote": "Gross bookings +8.4% MoM (July: 3 mid-market closes above $40k ACV)",
                "relevance": 0.78,
            },
        ],
    },
    "new_product_line_answer": {
        "kind": InterjectionKind.ANSWER,
        "headline": "New product line is down 12.1% MoM per the Q3 deck",
        "chat_alert": (
            "\U0001f5e3️ Kindred answered: the Q3 deck (p.14) puts the new product line "
            "at $1.42M, down 12.1% MoM. The +8% figure matches gross bookings, not net revenue."
        ),
        "body_md": (
            "The Q3 board deck reports the new product line at **$1.42M, down 12.1% "
            "month-over-month** (p.14).\n\n"
            "The +8% figure is real but it is a different metric: **gross bookings**, up "
            "8.4% MoM, driven by three mid-market closes above $40k ACV in July. The deck "
            "carries the net number into the board summary.\n\n"
            "So both numbers are defensible in isolation. They are not interchangeable, and "
            "the deck's definition is the net one."
        ),
        "confidence": 0.94,
        "citations": [
            {
                "document_id": DECK_DOC_ID,
                "filename": "Q3-board-deck.pdf",
                "chunk_id": f"{PREFIX_CHUNK}_01J8XK5B6C7D8E9F0G1H2J",
                "page": 14,
                "quote": "New Product Line: $1.42M (-12.1% MoM)",
                "relevance": 0.96,
            }
        ],
    },
    "midmarket_pricing_answer": {
        "kind": InterjectionKind.ANSWER,
        "headline": "Last quarter you deferred the mid-market repricing to Q4",
        "chat_alert": (
            "\U0001f5e3️ Kindred answered: last quarter the mid-market repricing was "
            "deferred to Q4 pending a churn read — the read is now in and churn is up "
            "to 4.1%."
        ),
        "body_md": (
            "In the Q2 product-line review the team agreed to **defer the mid-market "
            "repricing to Q4**, explicitly pending a churn read on the existing price "
            "point.\n\n"
            "Two things have changed since:\n\n"
            "- Monthly churn moved from **3.4% to 4.1%**, concentrated in mid-market.\n"
            "- The August churn analysis is still processing, so the pricing hypothesis "
            "has not actually been tested against it yet.\n\n"
            "The condition you set for revisiting this has been met."
        ),
        "confidence": 0.87,
        "citations": [
            {
                "document_id": NOTES_DOC_ID,
                "filename": "product-line-review-notes.md",
                "chunk_id": f"{PREFIX_CHUNK}_01J8XK5B6C7D8E9F0G1H4M",
                "page": None,
                "quote": "Mid-market repricing: defer to Q4, pending churn read on current price point.",
                "relevance": 0.89,
            }
        ],
    },
}


def _load_lines(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            events.append(json.loads(raw))
    return events


def list_fixtures() -> list[Fixture]:
    if not FIXTURES_DIR.exists():
        return []
    fixtures: list[Fixture] = []
    for path in sorted(FIXTURES_DIR.glob("*.jsonl")):
        events = _load_lines(path)
        meta = next((e for e in events if e.get("kind") == "meta"), {})
        participants = {e["participant_id"] for e in events if e.get("kind") == "participant_join"}
        fixtures.append(
            Fixture(
                id=meta.get("id", path.stem),
                title=meta.get("title", path.stem),
                description=meta.get("description", ""),
                duration_seconds=meta.get("duration_seconds", 0),
                participant_count=len(participants),
            )
        )
    return fixtures


def _fixture_path(fixture_id: str) -> Path | None:
    candidate = FIXTURES_DIR / f"{fixture_id}.jsonl"
    return candidate if candidate.exists() else None


def _partial_texts(text: str) -> list[str]:
    """Growing prefixes of an utterance, on word boundaries.

    Real STT revises as it goes; this only grows. Good enough to exercise the
    frontend's live-line rendering, which is what it exists for.
    """
    words = text.split()
    if len(words) <= 3:
        return []
    steps: list[str] = []
    cursor = 3
    while cursor < len(words):
        steps.append(" ".join(words[:cursor]))
        cursor += max(2, len(words) // 6)
    return steps


class HarnessRun:
    """One replay of one fixture into one meeting."""

    def __init__(self, meeting: Meeting, events: list[dict[str, Any]], speed: float, loop: bool):
        self.meeting = meeting
        self.events = events
        self.speed = speed
        self.loop = loop

    # --- emit helpers ------------------------------------------------------------

    def _publish(self, event_type: str, data: Any) -> None:
        bus.publish_meeting(self.meeting.id, event_type, data)

    def _set_agent_state(self, state: AgentState, detail: str | None = None) -> None:
        # A muted operator override wins over anything the fixture wants to do.
        if self.meeting.agent_state == AgentState.MUTED:
            return
        self.meeting.agent_state = state
        self._publish("agent.state_changed", AgentStateChangedData(agent_state=state, detail=detail))

    def _speaking(self, participant_id: str, is_speaking: bool) -> None:
        for entry in self.meeting.roster:
            if entry.participant_id == participant_id:
                entry.is_speaking = is_speaking
                break
        self._publish(
            "participant.speaking_changed",
            ParticipantSpeakingChangedData(
                participant_id=participant_id, is_speaking=is_speaking
            ),
        )

    def _roster_entry(self, participant_id: str) -> RosterEntry | None:
        return next(
            (e for e in self.meeting.roster if e.participant_id == participant_id), None
        )

    def _segment(
        self, participant_id: str, text: str, start_ms: int, end_ms: int, *, is_final: bool, seg_id: str
    ) -> TranscriptSegment:
        entry = self._roster_entry(participant_id)
        return TranscriptSegment(
            id=seg_id,
            meeting_id=self.meeting.id,
            participant_id=participant_id,
            person_id=entry.person_id if entry else None,
            speaker_name=entry.display_name if entry else participant_id,
            text=text,
            is_final=is_final,
            start_ms=start_ms,
            end_ms=end_ms,
            confidence=0.94 if is_final else None,
        )

    def _emit_interjection(self, ref: str, trigger: InterjectionTrigger, spoken: bool) -> Interjection:
        canned = _CANNED[ref]
        autonomy = store.settings.autonomy
        # `silent` and `propose` both hold at PROPOSED; the difference is whether the
        # operator's approval does anything downstream. Only `auto_post` reaches the
        # meeting without a human.
        posted = autonomy == Autonomy.AUTO_POST.value or autonomy == Autonomy.AUTO_POST
        status = InterjectionStatus.POSTED if posted else InterjectionStatus.PROPOSED
        interjection = store.build_interjection(
            self.meeting.id,
            kind=canned["kind"],
            status=status,
            chat_alert=canned["chat_alert"],
            headline=canned["headline"],
            body_md=canned["body_md"],
            confidence=canned["confidence"],
            trigger=trigger,
            citations=[Citation(**c) for c in canned["citations"]],
            spoken=spoken and posted,
        )
        store.interjections_for(self.meeting.id).append(interjection)
        self.meeting.stats.interjection_count += 1
        self._publish("interjection.proposed", interjection)
        return interjection

    # --- timeline construction ---------------------------------------------------

    def _build_timeline(self) -> list[tuple[int, Callable[[], None]]]:
        """Flatten the fixture into (at_ms, emit) pairs, then sort.

        Expanding first and sorting once is simpler to reason about than nesting
        sleeps inside per-event handlers, and it guarantees monotonic emission.
        """
        timeline: list[tuple[int, Callable[[], None]]] = []

        def add(at_ms: int, fn: Callable[[], None]) -> None:
            timeline.append((max(0, at_ms), fn))

        for event in self.events:
            kind = event.get("kind")
            if kind in (None, "meta"):
                continue
            at_ms = int(event.get("at_ms", 0))

            if kind == "participant_join":
                add(at_ms, lambda e=event: self._on_join(e))
            elif kind == "participant_leave":
                add(at_ms, lambda e=event: self._on_leave(e))
            elif kind in ("utterance", "wake"):
                self._add_utterance(add, event, at_ms, is_wake=kind == "wake")
            elif kind == "end":
                add(at_ms, self._on_end)

        timeline.sort(key=lambda pair: pair[0])
        return timeline

    def _add_utterance(
        self,
        add: Callable[[int, Callable[[], None]], None],
        event: dict[str, Any],
        at_ms: int,
        *,
        is_wake: bool,
    ) -> None:
        participant_id = event["participant_id"]
        text = event["text"]
        duration_ms = int(event.get("duration_ms", 3000))
        end_ms = at_ms + duration_ms
        seg_id = new_id(PREFIX_SEGMENT)

        add(at_ms, lambda: self._speaking(participant_id, True))

        offset = _PARTIAL_INTERVAL_MS
        for partial_text in _partial_texts(text):
            if offset >= duration_ms:
                break
            add(
                at_ms + offset,
                lambda t=partial_text, o=offset: self._publish(
                    "transcript.partial",
                    self._segment(
                        participant_id, t, at_ms, at_ms + o, is_final=False, seg_id=seg_id
                    ),
                ),
            )
            offset += _PARTIAL_INTERVAL_MS

        def emit_final() -> None:
            segment = self._segment(
                participant_id, text, at_ms, end_ms, is_final=True, seg_id=seg_id
            )
            store.upsert_segment(segment)
            self.meeting.stats.utterance_count += 1
            self.meeting.stats.duration_seconds = end_ms // 1000
            self._publish("transcript.final", segment)

        add(end_ms, emit_final)
        add(end_ms + 120, lambda: self._speaking(participant_id, False))

        if ref := event.get("triggers_interjection"):
            self._add_ambient_reasoning(add, end_ms, participant_id, text, ref, seg_id)

        if is_wake:
            self._add_wake_sequence(add, event, end_ms, participant_id, text, seg_id)

    def _add_ambient_reasoning(
        self,
        add: Callable[[int, Callable[[], None]], None],
        end_ms: int,
        participant_id: str,
        text: str,
        ref: str,
        seg_id: str,
    ) -> None:
        """Triage -> retrieve -> reason, as the operator sees it."""
        add(end_ms + 900, lambda: self._set_agent_state(AgentState.THINKING, "checking a claim"))

        def fire() -> None:
            entry = self._roster_entry(participant_id)
            self._emit_interjection(
                ref,
                InterjectionTrigger(
                    segment_ids=[seg_id],
                    person_id=entry.person_id if entry else None,
                    quote=text,
                ),
                spoken=False,
            )
            self._set_agent_state(AgentState.IDLE)

        add(end_ms + 5200, fire)

    def _add_wake_sequence(
        self,
        add: Callable[[int, Callable[[], None]], None],
        event: dict[str, Any],
        end_ms: int,
        participant_id: str,
        text: str,
        seg_id: str,
    ) -> None:
        """The full speech-mode state machine, on the timing budget from DESIGN.md §7.

        Wake fires on the *final* transcript, never a partial — partials produce
        false triggers, and this is the single likeliest thing to embarrass you
        on stage.
        """
        question = event.get("question", text)
        answer_ref = event["answer_ref"]
        wake_word = store.settings.wake_word

        def on_wake() -> None:
            if not store.settings.wake_word_enabled:
                return
            entry = self._roster_entry(participant_id)
            self._publish(
                "speech.wake_detected",
                WakeDetectedData(
                    participant_id=participant_id,
                    person_id=entry.person_id if entry else None,
                    segment_id=seg_id,
                    matched_text=wake_word,
                ),
            )
            self._set_agent_state(AgentState.LISTENING, "captured wake word")

        def on_question() -> None:
            if not store.settings.wake_word_enabled:
                return
            self._publish(
                "speech.question_captured",
                QuestionCapturedData(question=question, segment_ids=[seg_id]),
            )
            self._set_agent_state(AgentState.THINKING, "retrieving documents")

        def on_answer() -> None:
            if not store.settings.wake_word_enabled:
                return
            self._set_agent_state(AgentState.SPEAKING)
            entry = self._roster_entry(participant_id)
            interjection = self._emit_interjection(
                answer_ref,
                InterjectionTrigger(
                    segment_ids=[seg_id],
                    person_id=entry.person_id if entry else None,
                    quote=question,
                ),
                spoken=True,
            )
            self._publish("speech.answered", AnsweredData(interjection_id=interjection.id))

        add(end_ms, on_wake)
        add(end_ms + 1600, on_question)
        add(end_ms + 3300, on_answer)
        add(end_ms + 8300, lambda: self._set_agent_state(AgentState.IDLE))

    # --- lifecycle handlers ------------------------------------------------------

    def _on_join(self, event: dict[str, Any]) -> None:
        display_name = event["display_name"]
        person = store.person_by_name(display_name)
        entry = RosterEntry(
            participant_id=event["participant_id"],
            person_id=person.id if person else None,
            display_name=person.display_name if person else display_name,
            is_host=bool(event.get("is_host", False)),
            matched=person is not None,
            is_speaking=False,
        )
        self.meeting.roster.append(entry)
        self._publish("participant.joined", ParticipantJoinedData(participant=entry))

    def _on_leave(self, event: dict[str, Any]) -> None:
        participant_id = event["participant_id"]
        self.meeting.roster = [
            e for e in self.meeting.roster if e.participant_id != participant_id
        ]
        self._publish("participant.left", {"participant_id": participant_id})

    def _on_end(self) -> None:
        if self.loop:
            return
        self.meeting.state = MeetingState.ENDED
        self.meeting.ended_at = utcnow()
        self._set_agent_state(AgentState.IDLE)
        self._publish(
            "meeting.state_changed",
            {"state": MeetingState.ENDED.value, "agent_state": self.meeting.agent_state, "error": None},
        )

    # --- run ---------------------------------------------------------------------

    async def run(self) -> None:
        try:
            while True:
                timeline = self._build_timeline()
                elapsed_ms = 0
                for at_ms, emit in timeline:
                    delay = (at_ms - elapsed_ms) / 1000.0 / self.speed
                    if delay > 0:
                        await asyncio.sleep(delay)
                    elapsed_ms = at_ms
                    try:
                        emit()
                    except Exception:  # a bad fixture line must not kill the replay
                        log.exception("harness emit failed at %dms", at_ms)
                if not self.loop:
                    break
                self._reset_for_loop()
        except asyncio.CancelledError:
            log.info("harness run cancelled for %s", self.meeting.id)
            raise

    def _reset_for_loop(self) -> None:
        self.meeting.roster.clear()
        self.meeting.stats.utterance_count = 0
        self.meeting.stats.interjection_count = 0
        store.segments_for(self.meeting.id).clear()
        store.interjections_for(self.meeting.id).clear()


def start(fixture_id: str, speed: float = 1.0, loop: bool = False) -> Meeting:
    """Create a harness-backed meeting and begin replaying. Raises on unknown fixture."""
    path = _fixture_path(fixture_id)
    if path is None:
        raise FileNotFoundError(fixture_id)

    events = _load_lines(path)
    meta = next((e for e in events if e.get("kind") == "meta"), {})

    meeting = Meeting(
        id=store.new_meeting_id(),
        title=meta.get("title", fixture_id),
        meeting_url=None,
        state=MeetingState.IN_CALL,
        agent_state=AgentState.IDLE,
        source=MeetingSource.HARNESS,
        bot_id=None,
        roster=[],
        started_at=utcnow(),
    )
    store.meetings[meeting.id] = meeting

    run = HarnessRun(meeting, events, speed=speed, loop=loop)
    task = asyncio.create_task(run.run(), name=f"harness:{meeting.id}")
    store.harness_tasks[meeting.id] = task
    task.add_done_callback(lambda _t: store.harness_tasks.pop(meeting.id, None))

    bus.publish_global("meeting.state_changed", {
        "state": meeting.state,
        "agent_state": meeting.agent_state,
        "error": None,
    }, meeting_id=meeting.id)

    return meeting


def stop(meeting_id: str) -> Meeting | None:
    meeting = store.meetings.get(meeting_id)
    if meeting is None:
        return None
    if task := store.harness_tasks.pop(meeting_id, None):
        task.cancel()
    meeting.state = MeetingState.ENDED
    meeting.ended_at = utcnow()
    meeting.agent_state = AgentState.IDLE
    bus.publish_meeting(meeting_id, "meeting.state_changed", {
        "state": meeting.state,
        "agent_state": meeting.agent_state,
        "error": None,
    })
    return meeting
