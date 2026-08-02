"""Triage — is this utterance worth reasoning about at all?

The highest-QPS decision in the system: it runs on every utterance of every meeting,
forever. DESIGN.md §6 makes it a provider seam for exactly that reason.

The heuristic runs first and unconditionally, because it is free and it removes most of
the traffic. Only what survives reaches a model, and even then it is the cheap one. The
ordering is the whole optimization: you should not pay frontier-model prices to decide
whether "yeah, sounds good" is worth fact-checking.

**Two gates, not one, and the second one is why arguments get caught.** The original
gate asks "is there a checkable factual assertion here", which is the right question for
*a claim that conflicts with a document* and precisely the wrong one for *a person
disagreeing out loud*. Pushback is short, hedged, pronoun-heavy, and often phrased as a
question — every single one of the claim gate's drop rules:

    "No, that's not what the deck says."     7 words, no figure   → dropped
    "Wait, didn't we say four point one?"    ends in a question   → dropped
    "I think that number is wrong."          hedged               → dropped
    "That contradicts what Sarah just said." no assertive verb    → dropped

Ten of twelve real disagreement utterances died here, which meant the second half of
every argument was invisible to the model and a contradiction spanning two turns could
never be found. `looks_like_conflict` is the second gate: it looks for the shape of
someone pushing back rather than the shape of a claim, and either gate is enough to
earn a reasoning call.
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


MIN_CONFLICT_WORDS = 3
"""Floor for the conflict gate. "No it isn't" is three words and is an argument."""

# Someone saying the previous statement is wrong. These fire on their own.
_DISPUTE = re.compile(
    r"\b(disagree|disagrees|contradicts?|contradiction|contradictory|incorrect|mistaken|"
    r"not right|not true|not correct|not what|isn t right|isn t true|isn t what|"
    r"that s wrong|is wrong|are wrong|was wrong|the opposite|other way around|"
    r"doesn t match|don t match|doesn t square|off by|no it isn t|no it wasn t|"
    r"since when|says otherwise|beg to differ)\b"
)

# Pointing at something said or decided earlier. Half of a cross-turn contradiction.
# The optional adverb slot matters more than it looks: "we *already* decided" and "you
# *just* said" are how people actually phrase this, and requiring adjacency misses them.
_PRIOR_REFERENCE = re.compile(
    r"\b((i|you|we|she|he|they)\s+(already\s+|also\s+|just\s+|had\s+|have\s+|"
    r"literally\s+)?(said|told|agreed|decided|thought|remember|mentioned|called)|"
    r"didn t we|weren t we|wasn t it|wasn t that|a minute ago|earlier|"
    r"last (week|month|quarter|time|meeting)|"
    r"the (deck|doc|document|report|analysis|notes|model|data|numbers?) (says?|said|shows?|"
    r"showed|has|had))\b"
)

# Reversal markers. Cheap and common, so they only count alongside something else.
_CONTRAST = re.compile(r"\b(but|actually|however|though|except|whereas|instead)\b")
_CONTRAST_OPENER = re.compile(
    r"^(no|nope|but|wait|hold on|hang on|actually|however|hmm|sorry|um wait)\b"
)


def _normalize_for_conflict(text: str) -> str:
    """Lowercase, apostrophes stripped, so "isn't" and "isn t" match the same pattern."""
    return re.sub(r"[^\w\s]", " ", text.casefold())


def looks_like_conflict(text: str) -> bool:
    """Whether this utterance is somebody pushing back on what was just said.

    Deliberately more permissive than the claim gate. A false positive here costs one
    cheap model call and the model then says "none"; a false negative means the moment
    the user actually wants flagged — two people openly disagreeing — is never even
    looked at. DESIGN.md §6 makes that trade explicitly for triage.
    """
    words = text.split()
    if len(words) < MIN_CONFLICT_WORDS:
        return False

    normalized = _normalize_for_conflict(text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    if _DISPUTE.search(normalized):
        return True

    # A reversal on its own is far too common ("but anyway", "actually, moving on").
    # Paired with a pointer at something already said, or with a figure being restated,
    # it is the shape of a correction.
    reversal = bool(_CONTRAST.search(normalized)) or bool(_CONTRAST_OPENER.match(normalized))
    if not reversal:
        return False
    return bool(_PRIOR_REFERENCE.search(normalized)) or _has_figure(text)


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

    # Conflict-shaped speech skips the claim gate *and* the classifier. The classifier is
    # prompted to find factual assertions, so it says "not checkable" to "no, that's not
    # what the deck says" — correctly by its own lights, and fatally for the feature.
    # Whether that pushback is a real contradiction is the expensive model's call, and it
    # is the one holding the transcript needed to make it.
    if looks_like_conflict(text):
        log.debug("conflict-shaped, routing straight to reasoning: %r", text[:80])
        return True, 0.7

    if not heuristic_is_checkable(text):
        return False, 0.9

    provider_name = store.settings.triage.provider
    if provider_name == "heuristic":
        return True, 0.6

    # Note this ignores `triage.provider` beyond the `heuristic` check above: the
    # registry picks the backend, so triage rides whatever reasoning is using — including
    # Tenstorrent when `secure_meeting` is on. That is the point. Triage sees the raw
    # utterance, so routing reasoning away from the cloud while still shipping every
    # sentence to it for classification would leak exactly what the switch protects.
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
