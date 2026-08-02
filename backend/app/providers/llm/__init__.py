"""Reasoning providers, and the registry that picks one.

`get_llm_provider()` returns `None` when there is no API key. That is the load-bearing
part: the pipeline treats "no reasoning available" as a normal state and falls back to
the fixture's canned output, so the harness demo runs on a laptop with an empty `.env`.

The choice is resolved *per call*, not once per process, because the `secure_meeting`
setting can flip the backend mid-meeting from the dashboard. Clients are still built at
most once each and cached by name — flipping the toggle back and forth reuses the two
already-open httpx clients rather than leaking one per flip.
"""

from __future__ import annotations

import logging

from .base import LLMError, LLMProvider, LLMRefusal

logger = logging.getLogger(__name__)

SECURE_PROVIDER = "tenstorrent"
"""Where `secure_meeting` routes reasoning. Open-weight Qwen on Tenstorrent's own
hardware, rather than a third-party frontier API."""

_CLIENTS: dict[str, LLMProvider | None] = {}
"""Built providers, keyed by resolved name. `None` is cached too — a missing key is a
stable fact about the process, and re-deciding it would re-log the warning every call."""


def active_provider_name() -> str:
    """Which backend a reasoning call made right now would use. `none` means canned.

    Public because `/api/health` reports it, and reporting `config.resolved_llm_provider`
    there would be a lie the moment `secure_meeting` is on.
    """
    from ...config import get_config

    config = get_config()

    # Local import, same as `get_config` above: the provider layer is a leaf that the
    # rest of the app imports, and reaching back up to `app.store` at module scope would
    # make it one. `app/pipeline/triage.py` imports the store the same way.
    from ...store import store

    if store.settings.secure_meeting:
        # Deliberately not falling through to the cloud provider when the key is
        # missing. The point of the switch is that the transcript does not leave for a
        # third-party API; silently doing exactly that because a key is unset would be
        # the worst possible failure mode. `_build` logs and returns None, and the
        # pipeline degrades to canned output the same way it does with no keys at all.
        return SECURE_PROVIDER

    return config.resolved_llm_provider


def _build(resolved: str) -> LLMProvider | None:
    from ...config import get_config

    config = get_config()

    if resolved == "gemini":
        if not config.gemini_api_key:
            logger.warning("llm_provider is `gemini` but GEMINI_API_KEY is unset")
            return None
        from .gemini import GeminiProvider

        return GeminiProvider(
            api_key=config.gemini_api_key,
            model=config.gemini_model,
            fast_model=config.gemini_fast_model,
        )

    if resolved == "claude":
        if not config.anthropic_api_key:
            logger.warning("llm_provider is `claude` but ANTHROPIC_API_KEY is unset")
            return None
        try:
            from .claude import ClaudeProvider
        except ImportError:
            logger.warning(
                "ANTHROPIC_API_KEY is set but the `anthropic` package is not installed. "
                "Run `pip install -e .` in backend/."
            )
            return None

        return ClaudeProvider(
            api_key=config.anthropic_api_key,
            model=config.anthropic_model,
            fast_model=config.anthropic_fast_model,
        )

    if resolved == SECURE_PROVIDER:
        if not config.tenstorrent_api_key:
            logger.warning(
                "reasoning is routed to `tenstorrent` but TENSTORRENT_API_KEY is unset"
            )
            return None
        from .tenstorrent import TenstorrentProvider

        return TenstorrentProvider(
            api_key=config.tenstorrent_api_key,
            model=config.tenstorrent_model,
            fast_model=config.tenstorrent_fast_model,
            base_url=config.tenstorrent_base_url,
        )

    return None


def get_llm_provider() -> LLMProvider | None:
    """The provider this call should use, or None when it has no credentials."""
    resolved = active_provider_name()
    if resolved not in _CLIENTS:
        _CLIENTS[resolved] = _build(resolved)
    return _CLIENTS[resolved]


def _cache_clear() -> None:
    _CLIENTS.clear()


# `get_llm_provider` used to be `lru_cache`d and the tests reset it between cases.
# Keeping the attribute means the per-name cache above is a drop-in replacement.
get_llm_provider.cache_clear = _cache_clear  # type: ignore[attr-defined]


async def shutdown_llm_provider() -> None:
    """Close every client this process opened, not just the currently-selected one."""
    for provider in _CLIENTS.values():
        closer = getattr(provider, "aclose", None)
        if closer is not None:
            await closer()
    _cache_clear()


__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMRefusal",
    "SECURE_PROVIDER",
    "active_provider_name",
    "get_llm_provider",
    "shutdown_llm_provider",
]
