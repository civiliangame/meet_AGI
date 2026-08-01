"""Audio sinks — where a finished clip actually goes.

The sink is the swap point for how Kindred's voice reaches the meeting:

- `RecallAudioSink`  → `POST /bot/{id}/output_audio/`. Complete clips, one at a time.
  Simple, works today, and adds roughly a second before the audio is audible.
- `NullAudioSink`    → no meeting at all. Backs the fixture harness and `--dry-run`,
  so speech logic can be exercised with no API key and no bot.

The design doc's §7 upgrade — streaming at sentence granularity over Output Media — is
another implementation of this same protocol. Nothing in `app/speech/` needs to change
when it lands, which is the point of putting the seam here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .clip import AudioClip

if TYPE_CHECKING:
    # Type-only: importing the Recall client at runtime would make `app.audio` and
    # `app.integrations.recall` import each other in a cycle.
    from ..integrations.recall.client import RecallClient

logger = logging.getLogger(__name__)


@runtime_checkable
class AudioSink(Protocol):
    """Somewhere a clip can be played."""

    name: str

    async def wait_ready(self, timeout: float | None = None) -> None:
        """Block until playing a clip would actually be heard.

        A Recall bot sitting in a Google Meet waiting room accepts nothing; audio pushed
        before it is admitted is simply lost. Speech workers await this before every
        utterance so queued speech survives a slow admit instead of evaporating.
        """
        ...

    async def play(self, clip: AudioClip) -> None:
        """Hand the clip off for playback. Returns before playback finishes."""
        ...


class RecallAudioSink:
    """Plays into a live meeting through a Recall bot."""

    name = "recall"

    def __init__(self, client: "RecallClient", bot_id: str) -> None:
        self._client = client
        self._bot_id = bot_id
        self._ready = asyncio.Event()

    @property
    def bot_id(self) -> str:
        return self._bot_id

    def mark_ready(self) -> None:
        """Called by the session watcher once the bot is in-call and recording."""
        self._ready.set()

    def mark_not_ready(self) -> None:
        """Bot left, was removed, or dropped back to the waiting room."""
        self._ready.clear()

    async def wait_ready(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    async def play(self, clip: AudioClip) -> None:
        await self._client.output_audio(self._bot_id, clip.mp3)
        logger.info(
            "bot %s playing %sms clip %s", self._bot_id, clip.duration_ms, clip.clip_id or "<tts>"
        )


class NullAudioSink:
    """Accepts clips and drops them. Always ready.

    Used by the fixture harness and `--dry-run`, where the full speech path should run —
    queueing, state transitions, live events, timing — without a meeting to play into.
    """

    name = "null"

    def __init__(self, label: str = "dry-run") -> None:
        self._label = label

    async def wait_ready(self, timeout: float | None = None) -> None:
        return None

    async def play(self, clip: AudioClip) -> None:
        logger.info("[%s] would play %sms: %s", self._label, clip.duration_ms, clip.text)
