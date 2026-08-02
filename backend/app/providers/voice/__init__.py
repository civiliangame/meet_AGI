"""Voice providers, and the registry that picks one."""

from __future__ import annotations

import logging
from functools import lru_cache

from .base import VoiceError, VoiceProvider
from .sample import CHIME_CLIP_ID, SILENCE_CLIP_ID, SampleVoiceProvider

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_sample_clips() -> SampleVoiceProvider:
    """The pre-baked clips, whichever provider is doing the talking.

    They are assets on disk, not a TTS implementation, so `/speak {clip_id}` and the
    silent clip Recall requires at bot creation keep working with Inworld switched on.
    Cached because loading parses every mp3 to find its duration.
    """
    return SampleVoiceProvider()


# Named in the Settings contract but not implemented yet. Selecting one of these falls
# back to sample clips with a warning rather than failing: a missing voice provider
# should degrade Meet AGI's voice, not stop it from joining the meeting.
_PLANNED = {"elevenlabs", "system"}


def get_voice_provider(name: str | None = None) -> VoiceProvider:
    """Resolve a provider by name, defaulting to the configured one."""
    if name is None:
        from ...config import get_config

        name = get_config().voice_provider

    if name == "auto":
        # Semantics come from AppConfig.voice_provider's own docstring: prefer Inworld
        # when its key is present, otherwise fall back to sample clips. `auto` is the
        # config default, so leaving it unresolved here makes the whole app unbootable.
        from ...config import get_config

        name = "inworld" if get_config().inworld_api_key else "sample"

    if name == "sample":
        return get_sample_clips()

    if name == "inworld":
        from ...config import get_config

        config = get_config()
        if not config.inworld_api_key:
            logger.warning("voice provider `inworld` selected but INWORLD_API_KEY is unset")
            return get_sample_clips()
        from .inworld import InworldVoiceProvider

        return InworldVoiceProvider(
            api_key=config.inworld_api_key,
            voice_id=config.inworld_voice_id,
            model_id=config.inworld_model_id,
            base_url=config.inworld_base_url,
        )

    if name in _PLANNED:
        logger.warning("voice provider %r is not implemented yet — using sample clips", name)
        return get_sample_clips()
    raise VoiceError(
        f"unknown voice provider {name!r}; available: auto, inworld, sample, "
        f"{', '.join(sorted(_PLANNED))}"
    )


__all__ = [
    "CHIME_CLIP_ID",
    "SILENCE_CLIP_ID",
    "SampleVoiceProvider",
    "VoiceError",
    "VoiceProvider",
    "get_sample_clips",
    "get_voice_provider",
]
