"""The deterministic pieces of the pipeline: gate, chat cap, triage, retrieval.

No model calls here — these are the parts that must behave identically whether or not
an API key is present.
"""

from __future__ import annotations

import pytest

from app.chat.sinks import NullChatSink, fit_to_limit, with_trigger_prefix
from app.knowledge import get_knowledge_base
from app.pipeline.gate import InterjectionGate
from app.pipeline.triage import (
    heuristic_is_checkable,
    is_checkable_claim,
    looks_like_conflict,
)
from app.schemas import CHAT_ALERT_MAX_CHARS
from app.schemas.settings import TriageProviderName
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


class TestConflictGate:
    """The second triage gate, and the reason arguments now get flagged.

    The claim gate asks "is there a checkable factual assertion here". Pushback is short,
    hedged, pronoun-heavy and often a question, so it answered no to every one of these —
    and the second half of every argument died before reaching the model.
    """

    @pytest.mark.parametrize(
        "utterance",
        [
            "No, that's not what the deck says.",
            "Wait, didn't we say four point one percent?",
            "I think that number is wrong.",
            "That contradicts what Sarah just said.",
            "Hold on, that's not right.",
            "I disagree, mid-market is the problem.",
            "That's not what I remember from the board deck.",
            "We already decided to reprice, though.",
            "Actually the deck says it's down.",
            "No it isn't.",
            "Since when? I thought we were closer to a hundred and forty.",
        ],
    )
    def test_pushback_reaches_the_model(self, utterance: str) -> None:
        assert looks_like_conflict(utterance), utterance

    @pytest.mark.parametrize(
        "utterance",
        [
            # Reversals are far too common to fire on alone.
            "but anyway back to the roadmap",
            "actually let me share my screen",
            "okay let's move on",
            "yeah sounds good",
            "so I think we should ship it next week",
            "cool, thanks everyone",
            # Two words. "No it isn't" clears the floor; a bare "no" must not.
            "no",
            "nope",
        ],
    )
    def test_ordinary_speech_does_not(self, utterance: str) -> None:
        assert not looks_like_conflict(utterance), utterance

    def test_conflict_shape_bypasses_the_claim_gate(self) -> None:
        """Both gates are consulted, and either one is enough."""
        pushback = "No, that's not what the deck says."
        assert not heuristic_is_checkable(pushback), "the claim gate drops this"
        assert looks_like_conflict(pushback), "the conflict gate is what saves it"

    async def test_is_checkable_claim_routes_conflict_straight_through(
        self, monkeypatch
    ) -> None:
        """It must not be handed to the classifier, which is prompted to say no.

        The fast classifier looks for factual assertions. Asked whether "no, that's not
        what the deck says" is a checkable claim it answers no — correctly by its own
        lights, and fatally for the feature.
        """
        called = False

        def spy():
            nonlocal called
            called = True
            return None

        monkeypatch.setattr("app.pipeline.triage.get_llm_provider", spy)
        store.settings.triage.provider = TriageProviderName.CLAUDE

        checkable, _ = await is_checkable_claim("No, that's not what the deck says.")
        assert checkable
        assert not called, "conflict-shaped speech must skip the classifier entirely"


class TestRetrieval:
    def test_corpus_loaded_and_mapped_to_documents(self) -> None:
        kb = get_knowledge_base()
        assert kb.chunk_count > 0
        # Filenames come from the seeded Document records, not the .txt on disk, so the
        # frontend shows `Q3-board-deck.pdf` and citations resolve to a real document id.
        assert "Q3-board-deck.pdf" in kb.filenames

    def test_finds_both_sides_of_the_planted_contradiction(self) -> None:
        """Retrieval must surface both halves of a conflict, not just the one queried.

        This is a test of `knowledge/`, not of code. The corpus is demo content and can
        legitimately be swapped for a different scenario — so when the planted pair is
        gone this skips with a pointed message rather than failing, because a red test
        that means "you edited your demo data" trains people to ignore red tests.

        It is still worth having: there is no contradiction to detect unless the corpus
        holds both sides of one, and that is very easy to break by accident.
        """
        kb = get_knowledge_base()
        headings = " ".join(chunk.heading for chunk in kb.chunks)
        if "July bookings" not in headings:
            pytest.skip(
                "knowledge/product-line-review-notes.txt no longer contains the "
                "'July bookings' section holding the gross +8.4% figure. Nothing in the "
                "corpus now contradicts the deck's net -12.1%, so that demo beat cannot "
                "fire. Restore it, or plant a conflicting pair in the new corpus."
            )

        hits = kb.retrieve(
            "new product revenue is up about eight percent this quarter", top_k=6
        )
        retrieved = " ".join(chunk.heading for chunk, _ in hits)
        assert "Slide 14" in retrieved, "the net -12.1% figure must be retrievable"
        assert "July bookings" in retrieved, "the gross +8.4% figure must be retrievable"

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


class TestTriggerPrefix:
    """Every chat message names what prompted it, so it never reads as a non-sequitur."""

    def test_prepends_the_topic(self) -> None:
        assert with_trigger_prefix("Enterprise churn is 1.2%.", "enterprise churn") == (
            "Because you mentioned enterprise churn: enterprise churn is 1.2%."
        )

    def test_lowercases_the_first_word_so_it_reads_as_one_sentence(self) -> None:
        result = with_trigger_prefix("The Q3 deck says -12.1%.", "the revenue number")
        assert result.startswith("Because you mentioned the revenue number: the Q3 deck")

    def test_leaves_acronyms_alone(self) -> None:
        assert "ARR is flat" in with_trigger_prefix("ARR is flat", "revenue")

    def test_falls_back_when_the_model_omits_a_topic(self) -> None:
        assert with_trigger_prefix("Churn is up.", "") == "Because you mentioned that: churn is up."
        assert with_trigger_prefix("Churn is up.", None).startswith("Because you mentioned that:")

    def test_strips_stray_punctuation_and_case_from_the_topic(self) -> None:
        assert with_trigger_prefix("x", "  Mid-Market Churn.  ").startswith(
            "Because you mentioned mid-market churn:"
        )

    def test_is_idempotent(self) -> None:
        once = with_trigger_prefix("churn is up.", "churn")
        assert with_trigger_prefix(once, "churn") == once

    def test_still_fits_the_platform_limit(self) -> None:
        prefixed = with_trigger_prefix("word " * 300, "the revenue number")
        assert len(fit_to_limit(prefixed)) <= CHAT_ALERT_MAX_CHARS
        assert fit_to_limit(prefixed).startswith("Because you mentioned")
