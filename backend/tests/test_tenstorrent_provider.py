"""Pins the request shape and the failure handling of the Tenstorrent provider.

The three assertions worth keeping are the ones covering things the endpoint does
silently: thinking left on (slow enough to blow the speech budget), a schema that is
accepted but not applied, and a reasoning trace inlined into the content.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.providers.llm.base import LLMError, LLMRefusal
from app.providers.llm.tenstorrent import TenstorrentProvider

SCHEMA = {
    "type": "object",
    "properties": {"checkable": {"type": "boolean"}, "confidence": {"type": "number"}},
    "required": ["checkable", "confidence"],
    "additionalProperties": False,
}


def _provider(handler, **kwargs) -> TenstorrentProvider:
    return TenstorrentProvider(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _completion(content: str, *, finish: str = "stop", **message) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"finish_reason": finish, "message": {"content": content, **message}}
            ]
        },
    )


async def _call(provider: TenstorrentProvider, **kwargs) -> dict:
    return await provider.complete_json(
        system="classify", user="revenue was up eight percent", schema=SCHEMA, **kwargs
    )


async def test_request_shape_disables_thinking_and_pins_the_schema():
    """Thinking off and `json_schema` set — the two things the server will not do itself.

    With thinking on, a schema-constrained call took 125s and still ran out of tokens
    mid-object. Speech mode has a 2-3 second budget (DESIGN.md §7).
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["_auth"] = request.headers["Authorization"]
        seen["_url"] = str(request.url)
        return _completion('{"checkable": true, "confidence": 0.9}')

    result = await _call(_provider(handler))

    assert result == {"checkable": True, "confidence": 0.9}
    assert seen["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["schema"] == SCHEMA
    assert seen["response_format"]["json_schema"]["strict"] is True
    assert seen["messages"] == [
        {"role": "system", "content": "classify"},
        {"role": "user", "content": "revenue was up eight percent"},
    ]
    assert seen["_auth"] == "Bearer test-key"
    assert seen["_url"].endswith("/chat/completions")


async def test_effort_does_not_re_enable_thinking():
    """`effort` is part of the contract but must not turn thinking back on."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _completion('{"checkable": false, "confidence": 0.1}')

    await _call(_provider(handler), effort="max")

    assert seen["chat_template_kwargs"] == {"enable_thinking": False}


async def test_fast_selects_the_triage_model():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _completion('{"checkable": true, "confidence": 0.5}')

    provider = _provider(handler, model="Qwen/Qwen3-32B", fast_model="Qwen/Qwen3-Small")
    await _call(provider, fast=True)

    assert seen["model"] == "Qwen/Qwen3-Small"


async def test_inlined_thinking_and_markdown_fences_are_stripped():
    """`reasoning_content` is the documented shape; an inlined trace is the fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion(
            '<think>Let me consider the claim.</think>\n'
            '```json\n{"checkable": true, "confidence": 0.8}\n```',
            reasoning_content="ignored — the content field is what gets parsed",
        )

    assert await _call(_provider(handler)) == {"checkable": True, "confidence": 0.8}


async def test_truncated_thinking_trace_reports_the_real_cause():
    """A trace cut off by max_tokens must not surface as a JSON parse error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion("<think>Considering whether this is", finish="length")

    with pytest.raises(LLMError, match="thinking is still disabled"):
        await _call(_provider(handler))


async def test_content_filter_is_a_refusal_not_an_error():
    """A refusal must be distinguishable: retrying the same prompt will not help."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion("", finish="content_filter")

    with pytest.raises(LLMRefusal):
        await _call(_provider(handler))


@pytest.mark.parametrize(
    ("status", "match"),
    [
        (401, "TENSTORRENT_API_KEY"),
        (403, "TENSTORRENT_API_KEY"),
        (429, "rate-limited"),
        (404, "lists what this key can reach"),
        (500, "returned 500"),
    ],
)
async def test_http_errors_name_the_fix(status: int, match: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream said no")

    with pytest.raises(LLMError, match=match):
        await _call(_provider(handler))


async def test_unreachable_endpoint_is_an_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(LLMError, match="could not reach the Tenstorrent API"):
        await _call(_provider(handler))


async def test_non_object_json_is_rejected():
    """Guided decoding makes this near-impossible; a model that ignores the schema
    makes it merely unlikely, and the pipeline indexes the result as a dict."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion("[1, 2, 3]")

    with pytest.raises(LLMError, match="expected an object"):
        await _call(_provider(handler))


async def test_registry_builds_the_provider_when_the_flag_is_flipped(monkeypatch):
    """`LLM_PROVIDER=tenstorrent` is the flag; the key alone must not be enough."""
    from app.config import AppConfig, get_config
    from app.providers.llm import get_llm_provider

    def _use(**overrides):
        get_config.cache_clear()
        get_llm_provider.cache_clear()
        # `_env_file=None` so the repo-root `.env` cannot decide the outcome, and an
        # explicit `llm_provider` so the suite-wide `LLM_PROVIDER=none` (conftest) does
        # not answer for it either — env beats defaults, but not init kwargs.
        settings = {
            "llm_provider": "auto",
            "gemini_api_key": None,
            "anthropic_api_key": None,
            **overrides,
        }
        config = AppConfig(_env_file=None, **settings)
        monkeypatch.setattr("app.config.get_config", lambda: config)
        return config

    try:
        _use(llm_provider="tenstorrent", tenstorrent_api_key="k")
        provider = get_llm_provider()
        assert provider is not None and provider.name == "tenstorrent"

        # Flag flipped but no key: None, not a broken provider.
        _use(llm_provider="tenstorrent", tenstorrent_api_key=None)
        assert get_llm_provider() is None

        # Key present but the flag not flipped, alongside Gemini: Gemini still wins.
        config = _use(gemini_api_key="g", tenstorrent_api_key="k")
        assert config.resolved_llm_provider == "gemini"

        # Key present, nothing else set: `auto` falls through to Tenstorrent.
        config = _use(tenstorrent_api_key="k")
        assert config.resolved_llm_provider == "tenstorrent"
    finally:
        get_config.cache_clear()
        get_llm_provider.cache_clear()
