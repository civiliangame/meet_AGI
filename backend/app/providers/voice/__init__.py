"""Voice providers, and the registry that picks one."""

from __future__ import annotations

import logging

from .base import VoiceError, VoiceProvider
from .sample import CHIME_CLIP_ID, SILENCE_CLIP_ID, SampleVoiceProvider

logger = logging.getLogger(__name__)

# Named in the Settings contract but not implemented yet. Selecting one of these falls
# back to sample clips with a warning rather than failing: a missing voice provider
# should degrade Kindred's voice, not stop it from joining the meeting.
_PLANNED = {"inworld", "elevenlabs", "system"}


def get_voice_provider(name: str | None = None) -> VoiceProvider:
    """Resolve a provider by name, defaulting to the configured one."""
    if name is None:
        from ...config import get_config

        name = get_config().voice_provider

    if name == "sample":
        return SampleVoiceProvider()
    if name in _PLANNED:
        logger.warning("voice provider %r is not implemented yet — using sample clips", name)
        return SampleVoiceProvider()
    raise VoiceError(f"unknown voice provider {name!r}; available: sample, {', '.join(_PLANNED)}")


__all__ = [
    "CHIME_CLIP_ID",
    "SILENCE_CLIP_ID",
    "SampleVoiceProvider",
    "VoiceError",
    "VoiceProvider",
    "get_voice_provider",
]
