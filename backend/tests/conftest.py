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
    "GEMINI_API_KEY",
    "CHARACTERAI_API_KEY",
    "TENSTORRENT_API_KEY",
):
    os.environ[_var] = ""

# Pin both providers so the suite does not depend on which keys happen to be set.
os.environ["VOICE_PROVIDER"] = "sample"

# `none` keeps the fixture harness on its canned timeline. Without this, a developer
# with a reasoning key in `.env` runs a different code path than CI — the harness feeds
# the live pipeline, interjections arrive asynchronously seconds later, and every test
# that asserts on fixture output fails for reasons that have nothing to do with the
# change under test. Tests that want the pipeline patch in a stub explicitly.
os.environ["LLM_PROVIDER"] = "none"

import pytest  # noqa: E402

from app.config import get_config  # noqa: E402
from app.providers.llm import get_llm_provider  # noqa: E402
from app.providers.voice import get_voice_provider  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _clear_caches():
    """Drop memoized config and providers built before the env was pinned."""
    for fn in (get_config, get_voice_provider, get_llm_provider):
        cache_clear = getattr(fn, "cache_clear", None)
        if cache_clear is not None:
            cache_clear()
    yield


@pytest.fixture(autouse=True)
def _isolate_provider_cache():
    """Clear the memoized reasoning provider around every test.

    `get_llm_provider` is `lru_cache`d so the process builds one client, which is right
    in production and wrong in a suite: the first test to call it fixes the answer for
    every test after it. A test that flips `LLM_PROVIDER` and expects a different
    provider then passes alone and fails in the full run, purely on ordering.

    Clearing on the way out as well as in keeps a test's own override from leaking
    forward.
    """
    get_llm_provider.cache_clear()
    yield
    get_llm_provider.cache_clear()


@pytest.fixture(autouse=True)
def _no_live_credentials():
    """Guard the guard: fail loudly if a credential leaks back into the suite."""
    config = get_config()
    assert not config.recall_api_key, (
        "RECALL_API_KEY is set during tests — the suite would dispatch real bots. "
        "Check tests/conftest.py."
    )
    yield
