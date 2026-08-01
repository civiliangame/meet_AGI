"""The deterministic pieces of the pipeline: gate, chat cap, triage, retrieval.

No model calls here — these are the parts that must behave identically whether or not
an API key is present.
"""

from __future__ import annotations

import pytest

from app.chat.sinks import NullChatSink, fit_to_limit
from app.knowledge import get_knowledge_base
from app.pipeline.gate import InterjectionGate
from app.pipeline.triage import heuristic_is_checkable
from app.schemas import CHAT_ALERT_MAX_CHARS
from app.store import store


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def fresh_settings():
    """Each test gets the default policy; several mutate it."""
    store.reset()
    yield
    store.reset()


class TestGate:
    def test_allows_a_confident_first_interjection(self) -> None:
        gate = InterjectionGate(clock=FakeClock())
        assert gate.check("mtg_1", 0.9)

    def test_blocks_below_confidence_threshold(self) -> None:
        gate = InterjectionGate(clock=FakeClock())
        decision = gate.check("mtg_1", 0.3)
        assert not decision
        assert "confidence" in decision.reason

    def test_cooldown_blocks_then_expires(self) -> None:
        clock = FakeClock()
        gate = InterjectionGate(clock=clock)
        store.settings.interjection.cooldown_seconds = 90

        assert gate.check("mtg_1", 0.9)
        gate.record("mtg_1")

        assert not gate.check("mtg_1", 0.9)
        clock.advance(89)
        assert not gate.check("mtg_1", 0.9)
        clock.advance(2)
        assert gate.check("mtg_1", 0.9)

    def test_max_per_meeting(self) -> None:
        clock = FakeClock()
        gate = InterjectionGate(clock=clock)
        store.settings.interjection.max_per_meeting = 2
        store.settings.interjection.cooldown_seconds = 0

        for _ in range(2):
            assert gate.check("mtg_1", 0.9)
            gate.record("mtg_1")

        decision = gate.check("mtg_1", 0.99)
        assert not decision
        assert "already interjected" in decision.reason

    def test_meetings_are_independent(self) -> None:
        clock = FakeClock()
        gate = InterjectionGate(clock=clock)
        store.settings.interjection.cooldown_seconds = 90

        gate.record("mtg_1")
        assert not gate.check("mtg_1", 0.9)
        assert gate.check("mtg_2", 0.9), "one meeting's cooldown must not silence another"


class TestChatLimit:
    def test_short_message_passes_through(self) -> None:
        assert fit_to_limit("⚠️ revenue conflicts with the deck") == (
            "⚠️ revenue conflicts with the deck"
        )

    def test_collapses_whitespace(self) -> None:
        assert fit_to_limit("a\n\nb   c") == "a b c"

    def test_hard_caps_at_the_platform_limit(self) -> None:
        result = fit_to_limit("word " * 400)
        assert len(result) <= CHAT_ALERT_MAX_CHARS

    def test_truncates_on_a_word_boundary(self) -> None:
        result = fit_to_limit("alpha bravo " * 100)
        assert len(result) <= CHAT_ALERT_MAX_CHARS
        assert result.endswith("…")
        assert not result.rstrip("…").endswith(" ")
        # No half-word before the ellipsis.
        assert result.rstrip("…").split()[-1] in {"alpha", "bravo"}

    def test_single_long_token_is_still_capped(self) -> None:
        result = fit_to_limit("x" * 900)
        assert len(result) <= CHAT_ALERT_MAX_CHARS

    @pytest.mark.asyncio
    async def test_null_sink_enforces_the_cap(self) -> None:
        sink = NullChatSink()
        await sink.post("y " * 500)
        assert len(sink.sent[0]) <= CHAT_ALERT_MAX_CHARS


class TestTriageHeuristic:
    @pytest.mark.parametrize(
        "utterance",
        [
            "New product revenue is up about eight percent this quarter",
            "Churn is at four point one percent monthly, up from three point four",
            "We closed three mid-market deals above forty thousand ACV in July",
        ],
    )
    def test_claims_pass(self, utterance: str) -> None:
        assert heuristic_is_checkable(utterance)

    @pytest.mark.parametrize(
        "utterance",
        [
            "yeah",
            "sounds good",
            "Yeah yeah ok sure",
            "Good. Next, churn.",
            "And the new product line?",
            "What's driving it?",
            "I think we should revisit the pricing conversation",
        ],
    )
    def test_non_claims_are_dropped(self, utterance: str) -> None:
        assert not heuristic_is_checkable(utterance)

    def test_hedged_opinion_with_a_number_still_passes(self) -> None:
        """A figure is checkable even when it is wrapped in a hedge."""
        assert heuristic_is_checkable("I think revenue was up eight percent last quarter")

    @pytest.mark.parametrize(
        "utterance",
        [
            "Churn is four point one percent",
            "New product revenue is up eight percent",
            "We came in at eleven point four",
        ],
    )
    def test_short_numeric_claims_are_not_dropped(self, utterance: str) -> None:
        """Under the plain 8-word floor these vanish, and they are the whole point."""
        assert len(utterance.split()) < 8
        assert heuristic_is_checkable(utterance)

    def test_short_prose_without_a_figure_is_still_dropped(self) -> None:
        assert not heuristic_is_checkable("the pipeline looks healthy")


class TestRetrieval:
    def test_corpus_loaded_and_mapped_to_documents(self) -> None:
        kb = get_knowledge_base()
        assert kb.chunk_count > 0
        # Filenames come from the seeded Document records, not the .txt on disk, so the
        # frontend shows `Q3-board-deck.pdf` and citations resolve to a real document id.
        assert "Q3-board-deck.pdf" in kb.filenames

    def test_finds_both_sides_of_the_planted_contradiction(self) -> None:
        kb = get_knowledge_base()
        hits = kb.retrieve(
            "new product revenue is up about eight percent this quarter", top_k=6
        )
        headings = " ".join(chunk.heading for chunk, _ in hits)
        assert "Slide 14" in headings, "the net -12.1% figure must be retrievable"
        assert "July bookings" in headings, "the gross +8.4% figure must be retrievable"

    def test_citation_carries_a_real_document_id(self) -> None:
        kb = get_knowledge_base()
        chunk, score = kb.retrieve("new product line net revenue", top_k=1)[0]
        citation = chunk.as_citation(relevance=score)
        assert citation.document_id.startswith("doc_")
        assert citation.quote

    def test_unmatched_query_still_returns_context(self) -> None:
        """A pronoun-heavy question must not leave the model with nothing to read."""
        text, chunks = get_knowledge_base().context_for("what about that", top_k=3)
        assert chunks and text
