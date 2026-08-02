"""`AudioClip` — a piece of speech, ready to push into a meeting.

Every voice provider returns one of these and every audio sink consumes one, which is
the seam that lets the sample clips used today be swapped for Inworld TTS tomorrow
without anything in `app/speech/` changing.

mp3 because that is the only format Recall's audio-output endpoints accept.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from .mp3 import mp3_duration_ms


@dataclass(frozen=True)
class AudioClip:
    mp3: bytes
    text: str
    """What the audio actually says. Shown in the frontend timeline and in logs."""
    duration_ms: int
    clip_id: str | None = None
    """Set when the audio came from a fixed asset rather than being synthesized."""
    placeholder_for: str | None = field(default=None)
    """The text that was *requested*, when the audio says something else.

    The sample voice provider plays a canned line no matter what it is asked to say.
    Recording that here keeps the UI honest — it can show what Meet AGI meant to say and
    flag that the voice is still a stand-in, instead of silently lying about it.
    """

    @classmethod
    def from_mp3(
        cls,
        mp3: bytes,
        *,
        text: str,
        clip_id: str | None = None,
        placeholder_for: str | None = None,
    ) -> AudioClip:
        return cls(
            mp3=mp3,
            text=text,
            duration_ms=mp3_duration_ms(mp3),
            clip_id=clip_id,
            placeholder_for=placeholder_for,
        )

    @property
    def b64(self) -> str:
        """Standard-alphabet base64 (RFC 4648 §4), as Recall specifies."""
        return base64.b64encode(self.mp3).decode("ascii")

    @property
    def is_placeholder(self) -> bool:
        return self.placeholder_for is not None

    def __repr__(self) -> str:  # keeps log lines free of base64 spew
        return (
            f"AudioClip(clip_id={self.clip_id!r}, duration_ms={self.duration_ms}, "
            f"bytes={len(self.mp3)}, text={self.text[:40]!r})"
        )
