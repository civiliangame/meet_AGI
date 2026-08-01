"""Claude, via the Anthropic SDK.

Two settings here are deliberate and worth not "fixing" later:

**Adaptive thinking at low or medium effort, never disabled.** Speech mode has a 2-3
second budget from question-end to first audio (DESIGN.md §7), so the instinct is to
turn thinking off. Don't: on Claude Opus 5 a disabled-thinking request can emit a tool
call as plain text or leak internal tags into the response, and `effort: "low"` already
buys most of the latency back without either failure mode.

**Structured output on every call.** `output_config.format` pins the response to a JSON
schema, so the pipeline never parses prose and a malformed model response is impossible
rather than merely unlikely.

Refusals are surfaced as `LLMRefusal` instead of being papered over. The corpus is
finance documents so classifiers should never fire, but a refusal read as a normal
response would put an empty interjection in front of the meeting.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import LLMError, LLMRefusal

logger = logging.getLogger(__name__)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"
"""Server-side refusal fallback. On a policy decline the API re-runs the request on
Anthropic's recommended fallback model inside the same call, so a false-positive
classifier hit costs a little latency instead of the whole interjection."""


class ClaudeProvider:
    """Anthropic-backed reasoning."""

    name = "claude"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-opus-5",
        fast_model: str = "claude-haiku-4-5",
        refusal_fallbacks: bool = True,
    ) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._fast_model = fast_model
        self._fallbacks = refusal_fallbacks

    async def aclose(self) -> None:
        await self._client.close()

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
        import anthropic

        model = self._fast_model if fast else self._model
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            "thinking": {"type": "adaptive"},
        }

        try:
            response = await self._send(request)
        except anthropic.APIStatusError as exc:
            raise LLMError(f"{model} returned {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"could not reach the Anthropic API: {exc}") from exc

        # Check the stop reason before touching content: on a refusal `content` is empty
        # or partial, and indexing it would raise something unrelated to the real cause.
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise LLMRefusal(f"{model} declined this request (category={category})")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise LLMError(f"{model} returned no text (stop_reason={response.stop_reason})")

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:  # structured outputs make this near-impossible
            raise LLMError(f"{model} returned invalid JSON: {text[:200]}") from exc
        if not isinstance(payload, dict):
            raise LLMError(f"{model} returned {type(payload).__name__}, expected an object")
        return payload

    async def _send(self, request: dict[str, Any]):
        """Send with server-side refusal fallbacks, degrading if they are unavailable.

        The fallback parameter is beta and Claude-API-only. Rather than make the caller
        care, the first rejection turns it off for the life of the process and the
        request is retried on the plain endpoint.
        """
        import anthropic

        if self._fallbacks:
            try:
                return await self._client.beta.messages.create(
                    **request, betas=[_FALLBACK_BETA], fallbacks="default"
                )
            except anthropic.BadRequestError as exc:
                self._fallbacks = False
                logger.warning(
                    "server-side refusal fallbacks unavailable (%s); continuing without them",
                    exc,
                )
        return await self._client.messages.create(**request)
