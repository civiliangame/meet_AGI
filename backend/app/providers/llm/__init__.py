"""Reasoning providers, and the registry that picks one.

`get_llm_provider()` returns `None` when there is no API key. That is the load-bearing
part: the pipeline treats "no reasoning available" as a normal state and falls back to
the fixture's canned output, so the harness demo runs on a laptop with an empty `.env`.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from .base import LLMError, LLMProvider, LLMRefusal

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_llm_provider() -> LLMProvider | None:
    """The configured provider, or None when no credentials are set."""
    from ...config import get_config

    config = get_config()
    resolved = config.resolved_llm_provider

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

    if resolved == "tenstorrent":
        if not config.tenstorrent_api_key:
            logger.warning("llm_provider is `tenstorrent` but TENSTORRENT_API_KEY is unset")
            return None
        from .tenstorrent import TenstorrentProvider

        return TenstorrentProvider(
            api_key=config.tenstorrent_api_key,
            model=config.tenstorrent_model,
            fast_model=config.tenstorrent_fast_model,
            base_url=config.tenstorrent_base_url,
        )

    return None


async def shutdown_llm_provider() -> None:
    provider = get_llm_provider()
    closer = getattr(provider, "aclose", None)
    if closer is not None:
        await closer()
    get_llm_provider.cache_clear()


__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMRefusal",
    "get_llm_provider",
    "shutdown_llm_provider",
]
