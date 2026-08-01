"""Inworld TTS — Kindred's actual voice.

    POST https://api.inworld.ai/tts/v1/voice
    Authorization: Basic <INWORLD_API_KEY>

Requests mp3 explicitly. Recall's `output_audio` accepts only mp3, so asking Inworld for
anything else would mean transcoding in the middle of the latency budget.

Two limits from the API are enforced here rather than discovered at runtime: input is
capped at 2,000 characters, and `speakingRate` is only valid in [0.5, 1.5] while the
settings contract allows [0.5, 2.0]. Both are clamped, because a spoken answer that is
slightly clipped or slightly slow is a much better failure than a 400 mid-meeting.
"""

from __future__ import annotations

import base64
import binascii
import logging

import httpx

from ...audio import AudioClip, InvalidMp3
from .base import VoiceError

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 2000
"""Inworld's documented per-request input limit."""

_MIN_RATE, _MAX_RATE = 0.5, 1.5


class InworldVoiceProvider:
    """Real text-to-speech. The thing the product is named for."""

    name = "inworld"

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str = "Dennis",
        model_id: str = "inworld-tts-2",
        base_url: str = "https://api.inworld.ai",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._voice_id = voice_id
        self._model_id = model_id
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={
                # Inworld's scheme: the key is already the base64 credential, so it is
                # sent verbatim after `Basic` rather than being re-encoded.
                "Authorization": f"Basic {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        speaking_rate: float = 1.0,
    ) -> AudioClip:
        spoken = text.strip()
        if not spoken:
            raise VoiceError("nothing to synthesize: text was empty")
        if len(spoken) > MAX_INPUT_CHARS:
            logger.warning(
                "truncating %d chars to Inworld's %d-char limit", len(spoken), MAX_INPUT_CHARS
            )
            spoken = spoken[:MAX_INPUT_CHARS].rsplit(" ", 1)[0] + "..."

        payload = {
            "text": spoken,
            "voiceId": voice_id or self._voice_id,
            "modelId": self._model_id,
            "audioConfig": {
                "audioEncoding": "MP3",
                "speakingRate": min(max(speaking_rate, _MIN_RATE), _MAX_RATE),
            },
        }

        try:
            response = await self._client.post("/tts/v1/voice", json=payload)
        except httpx.HTTPError as exc:
            raise VoiceError(f"could not reach Inworld: {exc}") from exc

        if response.status_code in (401, 403):
            raise VoiceError(
                f"Inworld rejected the API key ({response.status_code}). The key is sent "
                f"verbatim after `Basic` — check INWORLD_API_KEY is the base64 credential "
                f"from the console, not a raw user:password pair."
            )
        if response.status_code >= 400:
            raise VoiceError(f"Inworld returned {response.status_code}: {response.text[:300]}")

        audio_b64 = response.json().get("audioContent")
        if not audio_b64:
            raise VoiceError("Inworld returned no audioContent")

        try:
            mp3 = base64.b64decode(audio_b64)
        except (binascii.Error, ValueError) as exc:
            raise VoiceError(f"Inworld returned undecodable audio: {exc}") from exc

        try:
            return AudioClip.from_mp3(mp3, text=spoken)
        except InvalidMp3 as exc:
            # Duration drives how long Kindred holds `speaking`, so this has to be real.
            raise VoiceError(f"Inworld returned audio that is not decodable mp3: {exc}") from exc
