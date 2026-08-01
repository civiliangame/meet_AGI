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

SHORT_CLAIM_MIN_WORDS = 5
"""The floor for an utterance that carries a figure. Short numeric claims are the most
checkable thing anyone says in a meeting, so they get a lower bar than prose."""

_BACKCHANNEL = frozenset(
    """
    yeah yes yep yup no nope ok okay sure right exactly totally agreed cool nice great
    thanks thank hello hi hey bye mhm uhhuh sounds good perfect got it makes sense fine
    correct absolutely definitely true indeed
    """.split()
)

_NUMERIC = re.compile(r"\d")

# Transcripts spell numbers out far more often than they write digits — the fixture says
# "up about eight percent", not "up 8%". A digits-only check would miss most of the
# figures people actually argue about in meetings.
_NUMBER_WORDS = frozenset(
    """
    zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen
    fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy
    eighty ninety hundred thousand million billion percent dollars quarter half double
    triple
    """.split()
)


def _has_figure(text: str) -> bool:
    """Whether the utterance contains a quantity, spelled out or in digits."""
    if _NUMERIC.search(text):
        return True
    return any(word.strip(".,!?%$").casefold() in _NUMBER_WORDS for word in text.split())


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

    # DESIGN.md §4 puts the floor at ~8 words, but the shortest utterances in a meeting
    # are often the most checkable: "churn is four point one percent" is six words and
    # is precisely the kind of claim this loop exists for. A figure buys an exemption.
    floor = SHORT_CLAIM_MIN_WORDS if _has_figure(text) else MIN_WORDS
    if len(words) < floor:
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
    if _HEDGES.search(text.casefold()) and not _has_figure(text):
        return False

    return bool(_has_figure(text) or _ASSERTIVE.search(text.casefold()))


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
