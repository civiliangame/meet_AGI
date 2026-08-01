"""Gemini, via the Generative Language REST API.

    POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent

Raw httpx rather than the `google-genai` SDK, to match how Inworld and Recall are wired
and to keep the dependency list short. The request shape is small and pinned by the
tests in `tests/test_gemini_provider.py`.

Three things about this API were verified against it rather than assumed, because
getting any of them wrong is a 400 at the worst possible moment:

1. **`additionalProperties` is rejected.** Gemini takes an OpenAPI *subset* as
   `responseSchema`, and the JSON Schema the rest of the pipeline uses carries keys it
   has never heard of. `_to_gemini_schema` whitelists what it accepts instead of
   blacklisting what it rejects, so a schema key added upstream later cannot break this.
2. **Thinking is `thinkingLevel`, not `thinkingBudget`.** The 2.5-era `thinkingBudget: 0`
   is a 400 on 3.x. `minimal` and `low` spend no thinking tokens at all; `medium` and
   `high` do.
3. **A safety block returns HTTP 200.** The refusal arrives as a `finishReason`, so the
   only way to notice is to check it before reading the text.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .base import LLMError, LLMRefusal

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# The subset of JSON Schema that Gemini's `responseSchema` accepts. Anything else is a
# 400 naming the offending key.
_ALLOWED_SCHEMA_KEYS = frozenset(
    {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "items",
        "properties",
        "required",
        "propertyOrdering",
        "minItems",
        "maxItems",
    }
)

# `effort` in the pipeline's vocabulary → Gemini's. `low` is the interesting one: it
# spends zero thinking tokens, which is what keeps speech mode inside its latency budget.
_THINKING_LEVEL = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}

# finishReason values that mean "the model declined", as opposed to "the model finished".
_REFUSAL_REASONS = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY"})


def _to_gemini_schema(schema: Any) -> Any:
    """Strip a JSON Schema down to the subset Gemini accepts."""
    if isinstance(schema, list):
        return [_to_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _ALLOWED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {name: _to_gemini_schema(sub) for name, sub in value.items()}
        elif key == "items":
            cleaned[key] = _to_gemini_schema(value)
        else:
            cleaned[key] = value
    return cleaned


class GeminiProvider:
    """Gemini-backed reasoning behind the same contract as every other provider."""

    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
        fast_model: str = "gemini-3.5-flash-lite",
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 45.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._fast_model = fast_model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"Content-Type": "application/json"},
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
        generation: dict[str, Any] = {
            "responseMimeType": "application/json",
            "responseSchema": _to_gemini_schema(schema),
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingLevel": _THINKING_LEVEL.get(effort, "low")},
        }
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation,
        }

        try:
            response = await self._client.post(
                f"/models/{model}:generateContent",
                params={"key": self._api_key},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"could not reach the Gemini API: {exc}") from exc

        if response.status_code in (401, 403):
            raise LLMError(
                f"Gemini rejected the API key ({response.status_code}). Check "
                f"GEMINI_API_KEY — note the value must not be wrapped in quotes."
            )
        if response.status_code == 429:
            raise LLMError(f"Gemini rate-limited this request: {response.text[:200]}")
        if response.status_code >= 400:
            raise LLMError(f"{model} returned {response.status_code}: {response.text[:300]}")

        body = response.json()

        # A prompt blocked before generation has no candidates at all.
        if blocked := body.get("promptFeedback", {}).get("blockReason"):
            raise LLMRefusal(f"{model} blocked the prompt ({blocked})")

        candidates = body.get("candidates") or []
        if not candidates:
            raise LLMError(f"{model} returned no candidates: {json.dumps(body)[:200]}")
        candidate = candidates[0]

        # Checked before the text is read: a safety block is an HTTP 200 whose content
        # is empty, so reading first would raise something unrelated to the real cause.
        finish = candidate.get("finishReason")
        if finish in _REFUSAL_REASONS:
            raise LLMRefusal(f"{model} declined this request (finishReason={finish})")

        parts = candidate.get("content", {}).get("parts") or []
        text = "".join(part["text"] for part in parts if "text" in part).strip()

        if finish == "MAX_TOKENS" and not text:
            raise LLMError(
                f"{model} hit maxOutputTokens ({max_tokens}) before emitting anything. "
                f"Raise max_tokens or lower the effort."
            )
        if not text:
            raise LLMError(f"{model} returned no text (finishReason={finish})")

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            # Truncation is the realistic cause: responseSchema makes the model emit
            # valid JSON, but the token cap can still cut it off mid-object.
            hint = " (response was truncated)" if finish == "MAX_TOKENS" else ""
            raise LLMError(f"{model} returned invalid JSON{hint}: {text[:200]}") from exc
        if not isinstance(parsed, dict):
            raise LLMError(f"{model} returned {type(parsed).__name__}, expected an object")
        return parsed
