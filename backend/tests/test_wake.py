"""Wake-word detection.

The highest-risk component in the demo (DESIGN.md §12), and the only one where both
failure directions are visible to an audience: a missed wake looks broken, a false wake
looks worse.
"""

from __future__ import annotations

import pytest

from app.pipeline.wake import StopDetector, WakeDetector, normalize


@pytest.fixture
def detector() -> WakeDetector:
    return WakeDetector("Hey AGI", ["Kindred"])


@pytest.fixture
def stopper() -> StopDetector:
    return StopDetector("Hey AGI", ["Kindred"])


class TestMatches:
    @pytest.mark.parametrize(
        "utterance",
        [
            "Hey AGI, what does the Q3 deck say about the new product line?",
            "hey agi what does the deck say",
            # STT writes initialisms out as separate letters far more often than not.
            "Hey A G I, what does the deck say?",
            "Hey A.G.I. what does the deck say?",
            # Homophones that real transcription produces for "AGI".
            "Hey Aji, what does the deck say?",
            "hey agee what does the deck say",
            # Punctuation and casing must not matter.
            "HEY AGI!!! what happened to churn?",
        ],
    )
    def test_wakes(self, detector: WakeDetector, utterance: str) -> None:
        assert detector.match(utterance) is not None, utterance

    def test_alias_still_wakes(self, detector: WakeDetector) -> None:
        """The fixture and the docs both use `Kindred`; it must keep working."""
        match = detector.match("Kindred, what does the Q3 deck actually say?")
        assert match is not None
        assert "what does the q3 deck actually say" in match.question

    def test_bare_wake_word_alone(self, detector: WakeDetector) -> None:
        match = detector.match("Hey AGI")
        assert match is not None
        assert not match.has_question

    def test_mid_sentence_when_addressed(self, detector: WakeDetector) -> None:
        """Turning to address the agent mid-utterance is a real and common pattern."""
        match = detector.match("Hold on. Hey AGI, what does the deck say about churn?")
        assert match is not None
        assert match.question.startswith("what does the deck say")


class TestFalsePositives:
    @pytest.mark.parametrize(
        "utterance",
        [
            # Someone discussing the product on stage — the demo-day nightmare.
            "we should call it hey agi",
            "the hey agi integration is what we are demoing",
            "I was talking to Kindred about this yesterday",
            "kindred spirits, the two of them",
        ],
    )
    def test_does_not_wake(self, detector: WakeDetector, utterance: str) -> None:
        assert detector.match(utterance) is None, utterance

    def test_unrelated_speech(self, detector: WakeDetector) -> None:
        assert detector.match("New product revenue is up about eight percent") is None


class TestQuestionExtraction:
    def test_strips_the_wake_phrase(self, detector: WakeDetector) -> None:
        match = detector.match("Hey AGI, remind me what we said about mid-market pricing.")
        assert match is not None
        assert match.question == "remind me what we said about mid market pricing"
        assert match.has_question

    def test_spelled_out_variant_consumed_fully(self, detector: WakeDetector) -> None:
        """The longest matching variant must win, or letters leak into the question."""
        match = detector.match("Hey A G I, what happened to churn?")
        assert match is not None
        assert match.question == "what happened to churn"

    def test_short_trailing_text_is_not_a_question(self, detector: WakeDetector) -> None:
        match = detector.match("Hey AGI, one sec")
        assert match is not None
        assert not match.has_question


class TestFuzzyVariants:
    """The homophone list is never complete. STT invents spellings nobody wrote down."""

    @pytest.mark.parametrize(
        "utterance",
        [
            "Hey Ajai, what was our growth last quarter?",
            "Hey Agie, what does the deck say?",
            "hey hgi what happened to churn",
            "Hey Aggi, what is the revenue number?",
        ],
    )
    def test_wakes_on_an_unlisted_mangling(self, detector: WakeDetector, utterance: str) -> None:
        match = detector.match(utterance)
        assert match is not None, utterance
        assert match.has_question

    @pytest.mark.parametrize(
        "utterance",
        [
            # Close to the wake word but not close enough, and none of them addressed.
            "hey Andy the numbers came in this morning",
            "hey Gigi has the deck landed",
            "the agenda item is revenue",
        ],
    )
    def test_does_not_invent_a_wake(self, detector: WakeDetector, utterance: str) -> None:
        assert detector.match(utterance) is None, utterance


class TestQuestionShape:
    """"Kick in when it hears a question it can answer" — this is that judgment."""

    @pytest.mark.parametrize(
        "utterance",
        [
            "Hey AGI, what's the answer to my previous question?",
            "Hey AGI, what was our growth last quarter?",
            "Hey AGI, remind me what churn was",
            # Two words is enough when the first one is a question word.
            "Hey AGI, what's churn?",
        ],
    )
    def test_recognizes_a_question(self, detector: WakeDetector, utterance: str) -> None:
        match = detector.match(utterance)
        assert match is not None, utterance
        assert match.has_question, match.question

    @pytest.mark.parametrize("utterance", ["Hey AGI", "Hey AGI, one sec", "Hey AGI, hang on"])
    def test_bare_wake_waits_for_the_question(
        self, detector: WakeDetector, utterance: str
    ) -> None:
        match = detector.match(utterance)
        assert match is not None
        assert not match.has_question


class TestStopPhrase:
    @pytest.mark.parametrize(
        "utterance",
        [
            "AGI stop talking",
            "AGI, stop talking.",
            "Hey AGI, stop.",
            "A.G.I. stop talking!",
            "Hey Aji, shut up",
            "okay AGI be quiet",
            "stop talking, AGI",
            "Kindred, that's enough",
            "AGI never mind",
        ],
    )
    def test_stops(self, stopper: StopDetector, utterance: str) -> None:
        assert stopper.match(utterance) is not None, utterance

    @pytest.mark.parametrize(
        "utterance",
        [
            # A stop verb with nobody's name on it is people talking to each other.
            "okay everyone stop talking over each other",
            "can we stop the rollout",
            "quiet down please",
            # The name with no stop verb anywhere.
            "Hey AGI, what does the deck say?",
            # The verb is about something else entirely, eight words from the name.
            "AGI gave us the revenue number so we should stop the rollout",
        ],
    )
    def test_does_not_stop(self, stopper: StopDetector, utterance: str) -> None:
        assert stopper.match(utterance) is None, utterance

    def test_reports_what_it_heard(self, stopper: StopDetector) -> None:
        match = stopper.match("Hey A G I, stop talking please")
        assert match is not None
        assert match.matched_text == "stop talking"

    def test_follows_a_custom_wake_word(self) -> None:
        stopper = StopDetector("Jarvis", [])
        assert stopper.match("Jarvis, stop talking") is not None
        assert stopper.match("AGI, stop talking") is None


class TestConfigurable:
    def test_custom_wake_word(self) -> None:
        detector = WakeDetector("Jarvis", [])
        assert detector.match("Jarvis, what is the revenue number?") is not None
        assert detector.match("Hey AGI, what is the revenue number?") is None

    def test_normalize_collapses_punctuation_and_space(self) -> None:
        assert normalize("  Hey,   A.G.I.!  ") == "hey a g i"
