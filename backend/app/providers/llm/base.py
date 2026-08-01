"""The reasoning seam.

One method — `complete_json` — because every call the pipeline makes wants a structured
verdict, never prose. Triage returns a boolean and a confidence; contradiction checking
returns a verdict with citations; speech mode returns an answer plus the short line to
say out loud. Forcing all of them through a JSON schema is what keeps the pipeline free
of output parsing.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class LLMError(RuntimeError):
    """The model could not be reached, or returned something unusable."""


class LLMRefusal(LLMError):
    """Safety classifiers declined the request.

    A distinct type because the response is different in kind: retrying the same prompt
    will not help, so the caller drops the interjection rather than backing off.
    """


@runtime_checkable
class LLMProvider(Protocol):
    name: str

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
        """Return a dict guaranteed to match `schema`.

        `effort` trades latency for depth. `fast` selects the cheap model — used by
        triage, which runs on every utterance in every meeting.
        """
        ...
