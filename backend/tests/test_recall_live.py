"""Recall real-time ingestion.

Recall streams transcript word-by-word. These cover the part that turns that stream
back into "a person finished a sentence", because everything downstream — wake
detection especially — is meaningless if it sees one word at a time.
"""

from __future__ import annotations

import asyncio

import pytest

from app.ingest.recall_live import RecallLiveIngest, join_words
from app.schemas import AgentState, Meeting, MeetingSource, MeetingState
from app.store import store

MEETING_ID = "mtg_live_test"
BOT_ID = "bot-abc-123"


@pytest.fixture
def meeting() -> Meeting:
    store.reset()
    record = Meeting(
        id=MEETING_ID,
        title="Live",
        state=MeetingState.IN_CALL,
        agent_state=AgentState.IDLE,
        source=MeetingSource.RECALL,
        bot_id=BOT_ID,
    )
    store.meetings[MEETING_ID] = record
    return record


@pytest.fixture
def captured(monkeypatch) -> list:
    """Intercept what reaches the pipeline."""
    segments: list = []
    monkeypatch.setattr(
        "app.pipeline.handle_final_segment", lambda seg: segments.append(seg)
    )
    return segments


def event(words, *, name="Priya Raman", pid=100, start=0.0, kind="transcript.data"):
    """A Recall real-time event, shaped exactly as their sample app documents it."""
    return {
        "event": kind,
        "data": {
            "data": {
                "participant": {
                    "id": pid,
                    "name": name,
                    "is_host": True,
                    "platform": "desktop",
                    "extra_data": {},
                },
                "words": [
                    {
                        "text": w,
                        "start_timestamp": {"relative": start + i * 0.3},
                        "end_timestamp": {"relative": start + i * 0.3 + 0.28},
                    }
                    for i, w in enumerate(words)
                ],
            },
            "realtime_endpoint": {"id": "rte_1", "metadata": {}},
            "transcript": {"id": "tr_1", "metadata": {}},
            "recording": {"id": "rec_1", "metadata": {}},
            "bot": {"id": BOT_ID, "metadata": {"meeting_id": MEETING_ID}},
        },
    }


class TestJoinWords:
    def test_reassembles_readable_text(self) -> None:
        words = [{"text": "Hey"}, {"text": "AGI"}, {"text": ","}, {"text": "what"}]
        assert join_words(words) == "Hey AGI, what"

    def test_no_space_before_terminal_punctuation(self) -> None:
        words = [{"text": "up"}, {"text": "eight"}, {"text": "percent"}, {"text": "."}]
        assert join_words(words) == "up eight percent."


@pytest.mark.asyncio
class TestAggregation:
    async def test_terminal_punctuation_flushes_immediately(self, meeting, captured) -> None:
        """A finished sentence should not wait out the silence timer."""
        ingest = RecallLiveIngest(silence_ms=10_000)
        await ingest.handle(event(["Hey", "AGI", ",", "what", "changed", "?"]))

        assert len(captured) == 1
        assert captured[0].text == "Hey AGI, what changed?"
        assert captured[0].is_final is True

    async def test_word_by_word_becomes_one_utterance(self, meeting, captured) -> None:
        """The failure this whole module exists to prevent.

        Recall streams a word per event. Without buffering, wake detection would be
        matching "Hey AGI" against the single word "hey".
        """
        ingest = RecallLiveIngest(silence_ms=80)
        for word in ["Hey", "AGI", "what", "does", "the", "deck", "say"]:
            await ingest.handle(event([word]))
            assert not captured, "must not emit mid-utterance"

        await asyncio.sleep(0.25)
        assert len(captured) == 1
        assert captured[0].text == "Hey AGI what does the deck say"

    async def test_silence_gap_separates_two_utterances(self, meeting, captured) -> None:
        ingest = RecallLiveIngest(silence_ms=80)

        await ingest.handle(event(["new", "product", "revenue", "is", "up"]))
        await asyncio.sleep(0.25)
        await ingest.handle(event(["the", "pipeline", "looks", "healthy"]))
        await asyncio.sleep(0.25)

        assert [s.text for s in captured] == [
            "new product revenue is up",
            "the pipeline looks healthy",
        ]
        assert captured[0].id != captured[1].id, "each utterance needs its own segment id"

    async def test_speakers_are_buffered_independently(self, meeting, captured) -> None:
        """Interleaved speech must not merge into one nonsense sentence."""
        ingest = RecallLiveIngest(silence_ms=80)

        await ingest.handle(event(["revenue"], name="Marcus Webb", pid=200))
        await ingest.handle(event(["hold"], name="Priya Raman", pid=100))
        await ingest.handle(event(["is", "up"], name="Marcus Webb", pid=200))
        await ingest.handle(event(["on"], name="Priya Raman", pid=100))
        await asyncio.sleep(0.25)

        by_speaker = {s.speaker_name: s.text for s in captured}
        assert by_speaker["Marcus Webb"] == "revenue is up"
        assert by_speaker["Priya Raman"] == "hold on"

    async def test_partials_never_reach_the_pipeline(self, meeting, captured) -> None:
        """DESIGN.md §5: waking on a partial is the likeliest on-stage embarrassment."""
        ingest = RecallLiveIngest(silence_ms=10_000)
        await ingest.handle(
            event(["Hey", "AG"], kind="transcript.partial_data")
        )
        await asyncio.sleep(0.05)
        assert not captured

    async def test_speaker_resolves_to_a_known_person(self, meeting, captured) -> None:
        ingest = RecallLiveIngest(silence_ms=10_000)
        await ingest.handle(event(["Churn", "is", "up", "."], name="Sarah Chen", pid=300))

        assert captured[0].speaker_name == "Sarah Chen"
        assert captured[0].person_id is not None, "should match the seeded Person"

    async def test_unknown_bot_is_ignored(self, meeting, captured) -> None:
        ingest = RecallLiveIngest(silence_ms=10)
        payload = event(["hello", "there", "."])
        payload["data"]["bot"] = {"id": "some-other-bot", "metadata": {}}

        await ingest.handle(payload)
        assert not captured


@pytest.mark.asyncio
class TestOpenMicrophone:
    """The bug: with a live mic nothing ever flushed, so Kindred answered nothing.

    A silence gap only arrives if there is silence. When the speaker leaves the mic
    open, words keep landing inside the gap and reset it, the buffer grows forever, and
    the wake word never reaches the pipeline — which is why it "only worked if you muted
    at the end".
    """

    async def test_a_speaker_who_never_pauses_still_flushes(self, meeting, captured) -> None:
        ingest = RecallLiveIngest(silence_ms=10_000, max_utterance_ms=300)

        # A word every 30ms, forever — no gap anywhere near the 10s silence timer.
        for word in ["so", "anyway", "the", "numbers", "were", "fine", "last", "month"]:
            await ingest.handle(event([word]))
            await asyncio.sleep(0.03)

        await asyncio.sleep(0.3)
        assert captured, "the ceiling must flush a buffer that never sees a gap"
        assert "so anyway" in captured[0].text

    async def test_a_question_lands_fast_when_the_mic_stays_open(
        self, meeting, captured
    ) -> None:
        """The demo case. Someone asks and keeps talking; the answer cannot wait."""
        ingest = RecallLiveIngest(
            silence_ms=10_000, max_utterance_ms=10_000, wake_max_ms=250
        )

        for word in ["Hey", "AGI", "what", "was", "our", "growth", "last", "quarter"]:
            await ingest.handle(event([word]))
        # Still talking — no silence to wait for.
        await asyncio.sleep(0.1)
        await ingest.handle(event(["um"]))
        await asyncio.sleep(0.4)

        assert len(captured) == 1
        assert captured[0].text.startswith("Hey AGI what was our growth last quarter")

    async def test_the_ceiling_is_absolute_not_resettable(self, meeting, captured) -> None:
        """Incoming words must not push the ceiling back, or it is no ceiling at all.

        This is the same shape as the bug it replaces: a deadline that every new word
        postpones is one an uninterrupted speaker never reaches.
        """
        ingest = RecallLiveIngest(silence_ms=10_000, max_utterance_ms=150)

        for _ in range(20):
            await ingest.handle(event(["and"]))
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.2)

        assert len(captured) >= 2, (
            "a continuous speaker should produce an utterance per ceiling window, "
            f"got {len(captured)}"
        )

    async def test_the_wake_gap_only_ever_shortens_the_wait(self, meeting, captured) -> None:
        """A deployment with a tighter silence gap keeps it after a wake word."""
        ingest = RecallLiveIngest(silence_ms=60, wake_silence_ms=5_000)

        await ingest.handle(event(["Hey", "AGI", "what", "changed"]))
        await asyncio.sleep(0.25)
        assert len(captured) == 1


@pytest.mark.asyncio
class TestKillPhrase:
    """"AGI, stop talking" — heard on partials, and never treated as an utterance."""

    @pytest.fixture
    def stopped(self, monkeypatch) -> list:
        calls: list = []

        async def fake_stop(meeting_id, *, source="someone", force=False):
            calls.append((meeting_id, source))
            return []

        from app.pipeline.engine import engine

        monkeypatch.setattr(engine, "handle_stop", fake_stop)
        return calls

    async def test_fires_on_a_partial(self, meeting, captured, stopped) -> None:
        """The whole point: do not wait for the sentence to finalize."""
        ingest = RecallLiveIngest(silence_ms=10_000)
        await ingest.handle(
            event(["AGI", "stop", "talking"], kind="transcript.partial_data")
        )

        assert stopped == [(MEETING_ID, "Priya Raman")]
        assert not captured

    async def test_fires_across_two_events(self, meeting, captured, stopped) -> None:
        """The name and the verb often arrive in separate word batches."""
        ingest = RecallLiveIngest(silence_ms=10_000)
        await ingest.handle(event(["Okay", "AGI"]))
        await ingest.handle(event(["stop", "talking"]))

        assert len(stopped) == 1

    async def test_never_reaches_the_pipeline_as_an_utterance(
        self, meeting, captured, stopped
    ) -> None:
        """It is not a question and not a claim. Reasoning about it is nonsense."""
        ingest = RecallLiveIngest(silence_ms=40)
        await ingest.handle(event(["AGI", "stop", "talking", "."]))
        await asyncio.sleep(0.2)

        assert stopped
        assert not captured

    async def test_ordinary_speech_does_not_stop_it(self, meeting, captured, stopped) -> None:
        ingest = RecallLiveIngest(silence_ms=40)
        await ingest.handle(event(["we", "should", "stop", "the", "rollout", "."]))
        await asyncio.sleep(0.2)

        assert not stopped
        assert len(captured) == 1


@pytest.mark.asyncio
class TestParticipants:
    async def test_join_and_leave_maintain_the_roster(self, meeting) -> None:
        ingest = RecallLiveIngest()
        base = event([], name="Sarah Chen", pid=300)

        base["event"] = "participant_events.join"
        await ingest.handle(base)
        assert [e.display_name for e in meeting.roster] == ["Sarah Chen"]
        assert meeting.roster[0].matched is True

        base["event"] = "participant_events.leave"
        await ingest.handle(base)
        assert meeting.roster == []

    async def test_speech_events_drive_the_speaking_indicator(self, meeting) -> None:
        ingest = RecallLiveIngest()
        base = event([], name="Sarah Chen", pid=300)

        base["event"] = "participant_events.join"
        await ingest.handle(base)

        base["event"] = "participant_events.speech_on"
        await ingest.handle(base)
        assert meeting.roster[0].is_speaking is True

        base["event"] = "participant_events.speech_off"
        await ingest.handle(base)
        assert meeting.roster[0].is_speaking is False
