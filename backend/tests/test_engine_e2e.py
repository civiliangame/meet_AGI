"""End-to-end through the engine with a stubbed model.

Everything between "a person finished speaking" and "Kindred spoke and typed" is
exercised for real: wake detection, retrieval, citation resolution, the gate, the
speech queue, and the chat sink. Only the model's judgment is stubbed, so these tests
run offline and deterministically.

What they are actually protecting is the wiring — that a wake word reaches speech mode
and produces audio, that an ordinary claim reaches the ambient loop and produces a
chat post, and that the two do not fire on the same utterance.
"""

from __future__ import annotations

import pytest

from app.chat import chat_router
from app.chat.sinks import NullChatSink
from app.config import AppConfig
from app.ids import PREFIX_SEGMENT, new_id
from app.knowledge import get_knowledge_base
from app.pipeline.engine import PipelineEngine
from app.runtime import build_runtime
from app.schemas import (
    AgentState,
    Autonomy,
    Meeting,
    MeetingSource,
    MeetingState,
    TranscriptSegment,
)
from app.store import store

MEETING_ID = "mtg_test_e2e"


class StubLLM:
    """Returns a canned structured response per prompt type. Records what it was asked."""

    name = "stub"

    def __init__(self, *, chunk_id: str, flag: bool = True) -> None:
        self.chunk_id = chunk_id
        self.flag = flag
        self.calls: list[str] = []

    async def complete_json(self, *, system, user, schema, **_kwargs):
        if "fast classifier" in system:
            self.calls.append("triage")
            return {"checkable": True, "confidence": 0.9}

        if "answer them out loud" in system:
            self.calls.append("answer")
            return {
                "spoken": "The Q3 deck puts the new product line down twelve percent, "
                "not up eight. The plus eight figure is gross bookings.",
                "chat_alert": "Q3 deck p.14: new product line $1.42M, -12.1% MoM. "
                "The +8% figure is gross bookings, not net revenue.",
                "headline": "New product line is down 12.1% MoM per the Q3 deck",
                "body_md": "The deck reports **-12.1% MoM**.",
                "confidence": 0.94,
                "chunk_ids": [self.chunk_id],
                "quotes": ["New Product Line: $1.42M (-12.1% MoM)."],
            }

        self.calls.append("ambient")
        if not self.flag:
            return {
                "verdict": "none",
                "confidence": 0.0,
                "headline": "",
                "chat_alert": "",
                "body_md": "",
                "chunk_ids": [],
                "quotes": [],
            }
        return {
            "verdict": "contradiction",
            "confidence": 0.86,
            "headline": "Marcus's revenue claim conflicts with the Q3 board deck",
            "chat_alert": "⚠️ Kindred: that +8% is gross bookings. The Q3 deck (p.14) "
            "has the new product line at -12.1% MoM on a net basis.",
            "body_md": "**Claim.** up ~8%.\n\n**Deck.** -12.1% MoM.",
            "chunk_ids": [self.chunk_id],
            "quotes": ["New Product Line: $1.42M (-12.1% MoM)."],
        }


@pytest.fixture
def deck_chunk_id() -> str:
    kb = get_knowledge_base()
    chunk = next(c for c in kb.chunks if "Slide 14" in c.heading)
    return chunk.id


@pytest.fixture
def meeting() -> Meeting:
    store.reset()
    record = Meeting(
        id=MEETING_ID,
        title="Q3 Revenue Review",
        state=MeetingState.IN_CALL,
        agent_state=AgentState.IDLE,
        source=MeetingSource.HARNESS,
    )
    store.meetings[MEETING_ID] = record
    store.settings.autonomy = Autonomy.AUTO_POST
    return record


@pytest.fixture
def chat() -> NullChatSink:
    sink = NullChatSink(label="test")
    chat_router.attach(MEETING_ID, sink)
    yield sink
    chat_router.detach(MEETING_ID)


@pytest.fixture
async def runtime(monkeypatch):
    """A runtime with sample audio and no credentials, so nothing hits the network.

    Async because attaching a speech channel starts its worker task, which needs a
    running loop.
    """
    import app.runtime as runtime_module

    built = build_runtime(
        AppConfig(voice_provider="sample", inworld_api_key=None, recall_api_key=None)
    )
    monkeypatch.setattr(runtime_module, "_runtime", built)
    built.attach_dry_run(MEETING_ID, label="test")
    yield built
    await built.speech.aclose()


@pytest.fixture
def engine(monkeypatch, deck_chunk_id: str) -> PipelineEngine:
    stub = StubLLM(chunk_id=deck_chunk_id)
    # `app.pipeline.engine` resolves to the singleton, not the module, so patch by path.
    monkeypatch.setattr("app.pipeline.reason.get_llm_provider", lambda: stub)
    monkeypatch.setattr("app.pipeline.triage.get_llm_provider", lambda: stub)

    instance = PipelineEngine()
    instance.stub = stub  # type: ignore[attr-defined]
    return instance


def _is_filler(utterance) -> bool:
    """Filler and ack audio, as opposed to something Kindred actually worked out.

    Under real TTS these are `filler:*`; with the sample provider standing in they are
    the fixed `checking` and `chime` clips.
    """
    clip_id = utterance.clip_id or ""
    return clip_id.startswith("filler:") or clip_id in {"checking", "chime"}


def segment(text: str, *, speaker: str = "Marcus Webb", participant: str = "p_3"):
    return TranscriptSegment(
        id=new_id(PREFIX_SEGMENT),
        meeting_id=MEETING_ID,
        participant_id=participant,
        person_id=None,
        speaker_name=speaker,
        text=text,
        is_final=True,
        start_ms=0,
        end_ms=4000,
        confidence=0.94,
    )


@pytest.mark.asyncio
class TestAmbientLoop:
    async def test_flags_a_contradiction_and_types_it_into_chat(
        self, meeting, chat, runtime, engine
    ) -> None:
        engine.handle_final_segment(
            segment("New product revenue is up about eight percent this quarter.")
        )
        await engine.drain()

        interjections = store.interjections_for(MEETING_ID)
        assert len(interjections) == 1
        flagged = interjections[0]
        assert flagged.kind == "contradiction"
        assert flagged.confidence == pytest.approx(0.86)

        # The citation resolved to a real seeded document, not the model's free text.
        assert flagged.citations
        assert flagged.citations[0].document_id.startswith("doc_")
        assert flagged.citations[0].page == 14

        # And it reached the meeting, inside the platform's limit.
        assert len(chat.sent) == 1
        assert len(chat.sent[0]) <= 500
        assert "12.1" in chat.sent[0]

    async def test_backchannel_never_reaches_the_model(
        self, meeting, chat, runtime, engine
    ) -> None:
        engine.handle_final_segment(segment("yeah, sounds good"))
        await engine.drain()

        assert engine.stub.calls == [], "the free heuristic must drop this before any call"
        assert not store.interjections_for(MEETING_ID)
        assert not chat.sent

    async def test_cooldown_suppresses_the_second_flag(
        self, meeting, chat, runtime, engine
    ) -> None:
        store.settings.interjection.cooldown_seconds = 300

        engine.handle_final_segment(segment("New product revenue is up eight percent."))
        await engine.drain()
        engine.handle_final_segment(segment("Core revenue came in at eleven point four."))
        await engine.drain()

        assert len(store.interjections_for(MEETING_ID)) == 1
        assert len(chat.sent) == 1
        assert engine.gate.stats(MEETING_ID)["suppressed"] == 1

    async def test_silent_autonomy_keeps_it_off_the_meeting(
        self, meeting, chat, runtime, engine
    ) -> None:
        store.settings.autonomy = Autonomy.SILENT

        engine.handle_final_segment(segment("New product revenue is up eight percent."))
        await engine.drain()

        # The dashboard still gets the reasoning; the meeting hears nothing.
        assert len(store.interjections_for(MEETING_ID)) == 1
        assert store.interjections_for(MEETING_ID)[0].status == "proposed"
        assert not chat.sent


@pytest.mark.asyncio
class TestSpeechMode:
    async def test_wake_word_answers_out_loud_and_in_chat(
        self, meeting, chat, runtime, engine
    ) -> None:
        engine.handle_final_segment(
            segment(
                "Hey AGI, what does the Q3 deck actually say about the new product line?",
                speaker="Priya Raman",
                participant="p_1",
            )
        )
        await engine.drain()
        # Deliberately does not wait for playback: what this test is about is *what*
        # Kindred decided to say, and blocking on ~12s of real clip duration only makes
        # it flaky under load. `test_filler_is_queued_before_the_answer` covers playback.

        answers = store.interjections_for(MEETING_ID)
        assert len(answers) == 1
        assert answers[0].kind == "answer"
        assert answers[0].spoken is True

        # It queued the ear-friendly line, not the headline. The sample voice provider
        # substitutes a canned clip and records what it was asked to say in
        # `requested_text`, so that is where the real answer lands under the stand-in.
        real = [u for u in runtime.speech.history(MEETING_ID) if not _is_filler(u)]
        assert real, "Kindred must actually queue audio"
        said = real[0].requested_text or real[0].text
        assert "twelve percent" in said
        assert "**" not in said and "p.14" not in said, "spoken text must be ear-friendly"

    async def test_filler_is_queued_before_the_answer(
        self, meeting, chat, runtime, engine
    ) -> None:
        """The room hears something immediately, and the answer waits its turn.

        Ordering is the whole feature: a filler that played *after* the answer would be
        worse than no filler. `SpeechOutput` serializes per meeting, so queueing the
        filler first is what guarantees it.
        """
        engine.handle_final_segment(
            segment("Hey AGI, what does the deck say about churn?", participant="p_1")
        )
        await engine.drain()

        history = runtime.speech.history(MEETING_ID)
        assert len(history) >= 2, "expected a filler and an answer"
        assert _is_filler(history[0]), f"first utterance should be filler, got {history[0].clip_id}"
        assert not _is_filler(history[1]), "the answer must come second"

        # Two sample clips of real duration play back to back here, so the budget is
        # generous on purpose — a tight one turns a correctness test into a load test.
        await runtime.speech.wait_until_idle(MEETING_ID, timeout=90)
        # Serialized: the filler finished before the answer was handed to the sink.
        assert history[0].played_at is not None
        assert history[1].played_at is not None
        assert history[0].played_at <= history[1].played_at

        assert len(chat.sent) == 1
        assert "p.14" in chat.sent[0]

    async def test_wake_word_does_not_also_run_the_ambient_loop(
        self, meeting, chat, runtime, engine
    ) -> None:
        engine.handle_final_segment(
            segment("Hey AGI, what does the deck say about churn?", participant="p_1")
        )
        await engine.drain()

        assert "ambient" not in engine.stub.calls
        assert engine.stub.calls.count("answer") == 1

    async def test_bare_wake_word_captures_the_next_utterance(
        self, meeting, chat, runtime, engine
    ) -> None:
        engine.handle_final_segment(segment("Hey AGI", participant="p_1"))
        await engine.drain()

        # Acknowledged out loud, but nothing answered yet.
        assert not store.interjections_for(MEETING_ID)
        assert any(u.clip_id == "chime" for u in runtime.speech.history(MEETING_ID))

        engine.handle_final_segment(
            segment("What does the deck say about the new product line?", participant="p_1")
        )
        await engine.drain()

        assert len(store.interjections_for(MEETING_ID)) == 1
        assert store.interjections_for(MEETING_ID)[0].kind == "answer"

    async def test_muted_agent_stays_silent(self, meeting, chat, runtime, engine) -> None:
        meeting.agent_state = AgentState.MUTED

        engine.handle_final_segment(
            segment("Hey AGI, what does the deck say?", participant="p_1")
        )
        await engine.drain()

        assert engine.stub.calls == []
        assert not store.interjections_for(MEETING_ID)
        assert not chat.sent


@pytest.mark.asyncio
async def test_harness_drives_the_real_pipeline(monkeypatch, deck_chunk_id, runtime) -> None:
    """The fixture replay must feed the live loop, not replay canned conclusions.

    This is the difference between a demo that *shows* a contradiction and one that
    *finds* it. With reasoning configured, the harness's own `triggers_interjection` and
    `answer_ref` hints must be ignored and every interjection must come from the model
    reading the transcript.
    """
    import asyncio

    from app.ingest import harness
    from app.pipeline.engine import engine as singleton

    store.reset()
    singleton.reset()

    stub = StubLLM(chunk_id=deck_chunk_id)
    for target in (
        "app.pipeline.reason.get_llm_provider",
        "app.pipeline.triage.get_llm_provider",
        "app.providers.llm.get_llm_provider",  # what harness._pipeline_available checks
    ):
        monkeypatch.setattr(target, lambda: stub)

    meeting = harness.start("q3_revenue_review", speed=40.0)
    assert meeting.source == "harness"

    try:
        # 162s of fixture at 40x, plus room for the reasoning tasks it spawns.
        await asyncio.sleep(6.0)
        await singleton.drain(timeout=15)

        assert stub.calls, "the harness never reached the pipeline"
        assert "answer" in stub.calls, "the fixture's two wake events must reach speech mode"

        interjections = store.interjections_for(meeting.id)
        assert interjections, "the live pipeline produced nothing"

        # Canned output cites hard-coded chunk ids that are not in the loaded corpus;
        # anything the live pipeline emits resolves against a real retrieved chunk.
        cited = [c.chunk_id for i in interjections for c in i.citations]
        assert deck_chunk_id in cited
    finally:
        harness.stop(meeting.id)
        singleton.reset()
        store.reset()
