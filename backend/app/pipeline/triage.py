"""Triage — is this utterance worth reasoning about at all?

The highest-QPS decision in the system: it runs on every utterance of every meeting,
forever. DESIGN.md §6 makes it a provider seam for exactly that reason.

The heuristic runs first and unconditionally, because it is free and it removes most of
the traffic. Only what survives reaches a model, and even then it is the cheap one. The
ordering is the whole optimization: you should not pay frontier-model prices to decide
whether "yeah, sounds good" is worth fact-checking.
"""

from __future__ import annotations

import logging
import re

from ..providers.llm import LLMError, get_llm_provider
from .prompts import TRIAGE_SCHEMA, TRIAGE_SYSTEM

log = logging.getLogger(__name__)

MIN_WORDS = 8
"""Utterances shorter than this are never checkable claims. DESIGN.md §4."""

_BACKCHANNEL = frozenset(
    """
    yeah yes yep yup no nope ok okay sure right exactly totally agreed cool nice great
    thanks thank hello hi hey bye mhm uhhuh sounds good perfect got it makes sense fine
    correct absolutely definitely true indeed
    """.split()
)

_NUMERIC = re.compile(r"\d")
_HEDGES = re.compile(
    r"\b(i think|i feel|we should|maybe|perhaps|probably|my sense|i'd say|i would say|"
    r"let's|shall we|can we|could we|what if|i propose|i suggest)\b"
)
_ASSERTIVE = re.compile(
    r"\b(is|are|was|were|has|have|had|did|came in at|hit|reached|grew|fell|dropped|rose|"
    r"increased|decreased|up|down|closed|shipped|agreed|decided|reported|shows|says)\b"
)


def heuristic_is_checkable(text: str) -> bool:
    """Free prefilter. Deliberately permissive — it only needs to drop the obvious."""
    words = text.split()
    if len(words) < MIN_WORDS:
        return False

    stripped = text.casefold().strip(" .,!?")
    if stripped in _BACKCHANNEL:
        return False
    # Short utterances made entirely of back-channel words, e.g. "yeah yeah ok sure".
    if len(words) <= 10 and all(w.strip(".,!?").casefold() in _BACKCHANNEL for w in words):
        return False

    if text.rstrip().endswith("?"):
        return False

    # A hedged proposal is an opinion, not a claim — unless it also carries a figure,
    # in which case the figure is still worth checking.
    if _HEDGES.search(text.casefold()) and not _NUMERIC.search(text):
        return False

    return bool(_NUMERIC.search(text) or _ASSERTIVE.search(text.casefold()))


async def is_checkable_claim(text: str) -> tuple[bool, float]:
    """Whether this utterance is worth the expensive reasoning call.

    Returns `(checkable, confidence)`. Falls back to the heuristic's verdict whenever the
    model is unavailable or errors — triage failing closed would silence the entire
    ambient loop, which is a far worse outcome than a few wasted reasoning calls.
    """
    from ..store import store

    if not heuristic_is_checkable(text):
        return False, 0.9

    provider_name = store.settings.triage.provider
    if provider_name == "heuristic":
        return True, 0.6

    # `tenstorrent` is a documented provider with no implementation yet (DESIGN.md §6).
    # It resolves to the cheap Claude model rather than failing, so flipping the setting
    # never breaks the pipeline.
    provider = get_llm_provider()
    if provider is None:
        return True, 0.6

    try:
        result = await provider.complete_json(
            system=TRIAGE_SYSTEM,
            user=text,
            schema=TRIAGE_SCHEMA,
            effort="low",
            max_tokens=1024,
            fast=True,
        )
    except LLMError as exc:
        log.debug("triage model unavailable (%s); using heuristic verdict", exc)
        return True, 0.6

    return bool(result.get("checkable")), float(result.get("confidence", 0.5))
