"""Filler speech — what Meet AGI says while it is still thinking.

Retrieval plus reasoning is one to three seconds. In a text UI that is nothing; in a
room full of people who just asked a question out loud it is a dead pause, and the
speaker starts wondering whether the thing heard them. So Meet AGI answers the *social*
question immediately ("yes, I'm on it") and the factual one when it has an answer.

**Rendered once, cached on disk.** These lines never change, so paying Inworld for them
on every wake would add latency to the exact moment the feature exists to protect. They
are synthesized on first use — or at startup, ahead of any meeting — and written to
`assets/audio/fillers/<voice>/`. Cache keys include the voice and model ids, so changing
either re-renders rather than serving audio in the old voice.

**The answer queues behind the filler, by construction.** `SpeechOutput` plays one clip
at a time per meeting, so queueing the filler first means the real answer waits for it
to finish. That is the intended behaviour, and it is why the filler lines are short.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from pathlib import Path

from ..audio import AudioClip, InvalidMp3
from ..config import ASSET_AUDIO_DIR
from ..providers.voice import VoiceError, VoiceProvider

logger = logging.getLogger(__name__)

FILLER_LINES: tuple[str, ...] = (
    "Sure thing, let me look that up for you.",
    "Great question, on it now.",
    "Okay, searching your internal documents.",
)

FILLER_DIR = ASSET_AUDIO_DIR / "fillers"

_SLUG = re.compile(r"[^a-z0-9]+")

SAMPLE_FILLER_CLIP_ID = "checking"
"""The stand-in when there is no real TTS: "Good question. Give me a second while I
check the documents." — already a filler line, so the sample path needs nothing new."""


def _slug(text: str) -> str:
    return _SLUG.sub("-", text.casefold()).strip("-")[:40]


class FillerBank:
    """Pre-rendered "still working on it" lines for one voice."""

    def __init__(
        self,
        voice: VoiceProvider,
        *,
        lines: tuple[str, ...] = FILLER_LINES,
        directory: Path | None = None,
        voice_id: str | None = None,
        seed: int | None = None,
    ) -> None:
        self._voice = voice
        self._lines = lines
        self._voice_id = voice_id or getattr(voice, "voice_id", None) or "default"
        self._dir = (directory or FILLER_DIR) / _slug(f"{voice.name}-{self._voice_id}")
        self._clips: dict[str, AudioClip] = {}
        self._random = random.Random(seed)
        self._last: str | None = None
        self._lock = asyncio.Lock()

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def synthesizes(self) -> bool:
        """False for the sample provider, which cannot say arbitrary text."""
        return getattr(self._voice, "name", "") != "sample"

    def _path(self, line: str) -> Path:
        # The hash disambiguates two lines that slug identically; the slug keeps the
        # filenames readable when someone opens the directory to check what it says.
        digest = hashlib.sha256(line.encode("utf-8")).hexdigest()[:8]
        return self._dir / f"{_slug(line)}-{digest}.mp3"

    async def warm(self) -> int:
        """Render and cache every line. Returns how many are available.

        Called at startup so the first wake of a meeting does not pay for synthesis.
        Failures are logged and skipped — a missing filler must never stop Meet AGI from
        answering, and the caller falls back to a sample clip.
        """
        if not self.synthesizes:
            return 0
        async with self._lock:
            for line in self._lines:
                try:
                    await self._load(line)
                except VoiceError as exc:
                    logger.warning("could not render filler %r: %s", line[:40], exc)
        return len(self._clips)

    async def _load(self, line: str) -> AudioClip:
        """One line, from memory, then disk, then the voice provider."""
        if cached := self._clips.get(line):
            return cached

        path = self._path(line)
        if path.exists():
            try:
                clip = AudioClip.from_mp3(path.read_bytes(), text=line, clip_id=f"filler:{_slug(line)}")
                self._clips[line] = clip
                return clip
            except (InvalidMp3, OSError) as exc:
                # A truncated file from an interrupted write. Drop it and re-render.
                logger.warning("discarding unreadable filler cache %s: %s", path.name, exc)
                path.unlink(missing_ok=True)

        clip = await self._voice.synthesize(line)
        clip = AudioClip(
            mp3=clip.mp3,
            text=line,
            duration_ms=clip.duration_ms,
            clip_id=f"filler:{_slug(line)}",
        )

        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            # Write via a temp file so an interrupted run cannot leave a half-written
            # mp3 that looks like a valid cache entry next time.
            temp = path.with_suffix(".mp3.part")
            temp.write_bytes(clip.mp3)
            temp.replace(path)
            logger.info("cached filler %r (%dms) at %s", line[:40], clip.duration_ms, path.name)
        except OSError as exc:
            logger.warning("could not cache filler to %s: %s", path, exc)

        self._clips[line] = clip
        return clip

    async def next_clip(self) -> AudioClip | None:
        """A filler line, never the same one twice in a row. None if unavailable."""
        if not self.synthesizes:
            return None

        candidates = [line for line in self._lines if line != self._last] or list(self._lines)
        if not candidates:
            return None
        line = self._random.choice(candidates)

        async with self._lock:
            try:
                clip = await self._load(line)
            except VoiceError as exc:
                logger.warning("filler unavailable, falling back to a sample clip: %s", exc)
                return None
        self._last = line
        return clip
