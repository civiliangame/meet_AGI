"""The voice provider seam.

`synthesize(text) -> AudioClip` is the entire contract. Inworld is the intended
implementation (voice is the premise of "a copilot that actually speaks"); the sample
provider standing in for it today satisfies the same protocol, so swapping one for the
other touches this package and nothing else.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ...audio import AudioClip


class VoiceError(RuntimeError):
    """Synthesis failed. Speech is skipped; the meeting carries on without it."""


@runtime_checkable
class VoiceProvider(Protocol):
    name: str

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        speaking_rate: float = 1.0,
    ) -> AudioClip:
        """Turn text into an mp3 clip.

        Implementations must return mp3 — it is the only format Recall's audio output
        accepts, so converting anywhere downstream would just mean converting twice.
        """
        ...
