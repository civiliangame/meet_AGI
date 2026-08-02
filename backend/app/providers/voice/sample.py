"""Sample voice — pre-baked clips, no API key, no network, no latency.

This is a stand-in for real TTS. `synthesize()` ignores the text it is given and returns
a canned line, recording the requested text in `AudioClip.placeholder_for` so the UI can
show what Meet AGI *meant* to say and label the voice as a stand-in rather than quietly
pretending the audio matches.

That makes the whole speech path — queueing, state transitions, bot dispatch, playback
timing — real and testable before Inworld is wired up, and it keeps the demo alive if
the TTS provider is down on the day.

Regenerate the clips with `python scripts/make_sample_audio.py`.
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path

from ...audio import AudioClip
from ...config import ASSET_AUDIO_DIR
from .base import VoiceError

logger = logging.getLogger(__name__)

SILENCE_CLIP_ID = "silence"
CHIME_CLIP_ID = "chime"


class SampleVoiceProvider:
    """Plays from a fixed set of clips on disk."""

    name = "sample"

    def __init__(self, asset_dir: Path | None = None, *, seed: int | None = None) -> None:
        self._dir = asset_dir or ASSET_AUDIO_DIR
        self._random = random.Random(seed)
        self._cache: dict[str, AudioClip] = {}
        self._last_played: str | None = None
        self._texts = self._load_manifest()

    def _load_manifest(self) -> dict[str, str]:
        manifest = self._dir / "manifest.json"
        if not manifest.exists():
            raise VoiceError(
                f"No sample clips at {self._dir}. Run `python scripts/make_sample_audio.py`."
            )
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return {entry["id"]: entry["text"] for entry in data["clips"]}

    # --- clip access --------------------------------------------------------------

    @property
    def clip_ids(self) -> list[str]:
        """Speakable clips, in manifest order. Excludes silence and the chime."""
        return list(self._texts)

    def clip(self, clip_id: str) -> AudioClip:
        """Load a clip by id. Cached — the mp3 and its duration are read once."""
        if clip_id in self._cache:
            return self._cache[clip_id]

        path = self._dir / f"{clip_id}.mp3"
        if not path.exists():
            raise VoiceError(
                f"No sample clip {clip_id!r} in {self._dir}. "
                f"Available: {', '.join(self.clip_ids) or '(none)'}"
            )
        clip = AudioClip.from_mp3(
            path.read_bytes(),
            text=self._texts.get(clip_id, clip_id),
            clip_id=clip_id,
        )
        self._cache[clip_id] = clip
        return clip

    def random_clip(self) -> AudioClip:
        """A clip at random, never the same one twice in a row.

        Immediate repeats are what make a canned-audio demo read as broken, and avoiding
        them costs one line.
        """
        candidates = [cid for cid in self.clip_ids if cid != self._last_played]
        if not candidates:
            candidates = self.clip_ids
        if not candidates:
            raise VoiceError("no sample clips available")
        chosen = self._random.choice(candidates)
        self._last_played = chosen
        return self.clip(chosen)

    def silence(self) -> AudioClip:
        """A silent clip.

        Load-bearing, not filler: Recall refuses `output_audio` unless the bot was
        created with `automatic_audio_output`, so a bot with nothing to announce on join
        gets this in that slot to keep the on-demand path open.
        """
        return self.clip(SILENCE_CLIP_ID)

    def chime(self) -> AudioClip:
        """Short ack tone. Played on wake, before Meet AGI has an answer to give."""
        return self.clip(CHIME_CLIP_ID)

    # --- VoiceProvider ------------------------------------------------------------

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        speaking_rate: float = 1.0,
    ) -> AudioClip:
        """Return a canned clip, tagged with the text that was actually requested."""
        clip = self.random_clip()
        logger.info("sample voice: substituting %r for requested text %r", clip.clip_id, text[:80])
        return AudioClip(
            mp3=clip.mp3,
            text=clip.text,
            duration_ms=clip.duration_ms,
            clip_id=clip.clip_id,
            placeholder_for=text,
        )
