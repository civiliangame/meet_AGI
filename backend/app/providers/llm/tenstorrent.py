"""Qwen on Tenstorrent hardware, via the OpenAI-compatible console endpoint.

    POST https://console.tenstorrent.com/v1/chat/completions

Raw httpx rather than the `openai` SDK, to match how Gemini, Inworld and Recall are
wired and to keep the dependency list short. The request shape is small and pinned by
the tests in `tests/test_tenstorrent_provider.py`.

Three things about this endpoint were verified against it rather than assumed, because
each of them fails in a way that looks like something else:

1. **Thinking must be turned off explicitly, via `chat_template_kwargs`.** Qwen3 is a
   hybrid-reasoning model and the server leaves thinking *on*. A schema-constrained
   request with thinking enabled took 125 seconds and still ran out of tokens mid-object
   — the reasoning trace spends the entire `max_tokens` budget before the answer starts,
   so it reads as "the model returned truncated JSON" rather than "thinking is on". The
   same request with thinking off is ~2.7s. Speech mode has a 2-3 second budget from
   question-end to first audio (DESIGN.md §7), so this is not a tuning preference.

2. **`response_format` is accepted by every model but only *enforced* by some.**
   `Qwen/Qwen3-32B` honours `json_schema`: three identical calls returned the exact
   requested field set, including deliberately invented field names it had no way to
   guess. `Qwen/Qwen3-VL-32B-Instruct` — the newer model — returns HTTP 200 and ignores
   the schema completely, answering in whatever shape it likes. Nothing in the response
   says it was ignored. See `AppConfig.tenstorrent_model` for why the older model is the
   default.

3. **Reasoning comes back in its own field.** When thinking does run, the trace arrives
   as `message.reasoning_content` and `content` stays clean JSON. `_strip_thinking` is
   belt-and-braces for a server build that inlines `<think>` into the content instead.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .base import LLMError, LLMRefusal

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://console.tenstorrent.com/v1"

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _strip_thinking(text: str) -> str:
    """Remove an inlined `<think>` trace and any markdown fence around the JSON.

    Neither should appear: thinking is disabled and `response_format` suppresses prose.
    Both are cheap to strip and turn a hard parse failure into a working response if a
    server build ever stops honouring one of those.
    """
    text = _THINK_BLOCK.sub("", text)
    # An unterminated trace — the model started thinking and the token cap cut it off.
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return _FENCE.sub("", text.strip()).strip()


class TenstorrentProvider:
    """Tenstorrent-backed reasoning behind the same contract as every other provider."""

    name = "tenstorrent"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "Qwen/Qwen3-32B",
        fast_model: str = "Qwen/Qwen3-32B",
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._fast_model = fast_model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        effort: str = "low",
        max_tokens: int = 4096,
        fast: bool = False,
    ) -> dict[str, Any]:
        model = self._fast_model if fast else self._model
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            # Guided decoding makes the output deterministic anyway; 0.0 keeps triage
            # from flip-flopping on the same utterance between runs.
            "temperature": 0.0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": True},
            },
            # See the module docstring, point 1. `effort` is accepted for contract
            # compatibility and deliberately ignored: there is no setting between "off"
            # and "125 seconds", so every call takes the fast path.
            "chat_template_kwargs": {"enable_thinking": False},
        }

        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"could not reach the Tenstorrent API: {exc}") from exc

        if response.status_code in (401, 403):
            raise LLMError(
                f"Tenstorrent rejected the API key ({response.status_code}). Check "
                f"TENSTORRENT_API_KEY."
            )
        if response.status_code == 429:
            raise LLMError(f"Tenstorrent rate-limited this request: {response.text[:200]}")
        if response.status_code == 404:
            raise LLMError(
                f"Tenstorrent has no model `{model}` (404). `GET "
                f"{self._client.base_url}/models` lists what this key can reach."
            )
        if response.status_code >= 400:
            raise LLMError(f"{model} returned {response.status_code}: {response.text[:300]}")

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"{model} returned no choices: {json.dumps(body)[:200]}")
        choice = choices[0]
        finish = choice.get("finish_reason")

        # Checked before the text is read: on a filter hit the content is empty, and
        # indexing it would raise something unrelated to the real cause.
        if finish == "content_filter":
            raise LLMRefusal(f"{model} declined this request (finish_reason={finish})")

        text = _strip_thinking(choice.get("message", {}).get("content") or "")

        if finish == "length" and not text:
            raise LLMError(
                f"{model} hit max_tokens ({max_tokens}) before emitting anything. Raise "
                f"max_tokens — or check that thinking is still disabled, because a "
                f"reasoning trace will spend the whole budget before the answer starts."
            )
        if not text:
            raise LLMError(f"{model} returned no text (finish_reason={finish})")

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            hint = " (response was truncated)" if finish == "length" else ""
            raise LLMError(f"{model} returned invalid JSON{hint}: {text[:200]}") from exc
        if not isinstance(parsed, dict):
            raise LLMError(f"{model} returned {type(parsed).__name__}, expected an object")
        return parsed
