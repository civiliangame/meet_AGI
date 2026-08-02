"""Speech output contract — what Meet AGI said out loud, and how to ask it to speak.

An `Utterance` is one clip of audio played into a meeting. It is deliberately separate
from `Interjection`: an interjection is a *conclusion* (with citations, confidence, a
chat alert), while an utterance is the *audio event* that carried it. One interjection
that is both posted to chat and spoken produces one interjection and one utterance.

The frontend uses the utterance stream to show what the meeting actually heard, which is
not always what Meet AGI meant to say while the voice provider is a stand-in — hence
`placeholder`.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .common import Schema, Ts


class UtteranceStatus(str, Enum):
    QUEUED = "queued"
    """Waiting its turn. Meet AGI plays one clip at a time, in order."""
    SPEAKING = "speaking"
    """Handed to the meeting; audio is playing right now."""
    PLAYED = "played"
    DROPPED = "dropped"
    """Deliberately not played — Meet AGI was muted, or the bot never became available."""
    FAILED = "failed"
    """Synthesis or playback errored. `error` says which."""


class Utterance(Schema):
    id: str = Field(examples=["utt_01J8XP6N7P8Q9R0S1T2U3V"])
    meeting_id: str
    text: str = Field(
        description="What the audio actually says.",
        examples=["Quick flag. That revenue number conflicts with the Q3 board deck."],
    )
    requested_text: str | None = Field(
        default=None,
        description=(
            "What the caller asked Meet AGI to say, when that differs from `text`. "
            "Non-null only while the voice provider is a placeholder."
        ),
    )
    clip_id: str | None = Field(
        default=None,
        description="Set when the audio came from a fixed sample clip rather than TTS.",
        examples=["flag_revenue"],
    )
    placeholder: bool = Field(
        default=False,
        description=(
            "True when the audio is a stand-in that does not say `requested_text`. "
            "The UI should label it rather than presenting it as real speech."
        ),
    )
    duration_ms: int = 0
    status: UtteranceStatus = UtteranceStatus.QUEUED
    error: str | None = None
    created_at: Ts
    played_at: Ts | None = None


class SpeakRequest(Schema):
    """Ask Meet AGI to say something out loud in a meeting.

    Exactly one of `text` or `clip_id`. `text` goes through the voice provider;
    `clip_id` plays a known sample clip verbatim, which is what the demo path uses and
    what makes a stage failure recoverable.
    """

    text: str | None = Field(default=None, max_length=2000)
    clip_id: str | None = Field(
        default=None,
        description="A sample clip id. `GET /api/speech/clips` lists them.",
        examples=["greeting"],
    )


class SpeakRandomRequest(Schema):
    """Play random sample clips — the smoke test for the whole audio-output path."""

    count: int = Field(default=1, ge=1, le=20)
    gap_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=60.0,
        description="Silence between clips, on top of each clip's own length.",
    )


class SampleClip(Schema):
    id: str = Field(examples=["greeting"])
    text: str
    duration_ms: int


class ClipList(Schema):
    provider: str = Field(description="Active voice provider.", examples=["sample"])
    items: list[SampleClip]
