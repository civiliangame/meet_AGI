"""Test environment isolation.

**This module must run before `app` is imported**, which it does: pytest imports
`conftest.py` ahead of test modules.

The repo-root `.env` holds real credentials. `AppConfig` reads it, so without this file
the suite would pick up a live `RECALL_API_KEY` and `POST /api/meetings` would dispatch
a real, billable Recall bot to whatever URL a test happened to pass. Tests must never
touch an external service, and they must produce the same result on a machine that has
no `.env` at all.

Environment variables take precedence over the `.env` file in pydantic-settings, so
blanking them here wins. The provider caches are cleared as well, in case an earlier
import already resolved a config.
"""

from __future__ import annotations

import os

# Blank every outbound credential before anything reads config.
for _var in (
    "RECALL_API_KEY",
    "INWORLD_API_KEY",
    "ANTHROPIC_API_KEY",
    "CHARACTERAI_API_KEY",
    "TENSTORRENT_ENDPOINT",
):
    os.environ[_var] = ""

# Pin the voice provider so the suite does not depend on which keys happen to be set.
os.environ["VOICE_PROVIDER"] = "sample"

import pytest  # noqa: E402

from app.config import get_config  # noqa: E402
from app.providers.voice import get_voice_provider  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _clear_caches():
    """Drop memoized config and providers built before the env was pinned."""
    for fn in (get_config, get_voice_provider):
        cache_clear = getattr(fn, "cache_clear", None)
        if cache_clear is not None:
            cache_clear()
    yield


@pytest.fixture(autouse=True)
def _no_live_credentials():
    """Guard the guard: fail loudly if a credential leaks back into the suite."""
    config = get_config()
    assert not config.recall_api_key, (
        "RECALL_API_KEY is set during tests — the suite would dispatch real bots. "
        "Check tests/conftest.py."
    )
    yield
