"""Triage — is this stretch of conversation worth reasoning about?

The highest-QPS decision in the system: it runs on every utterance of every meeting,
forever. DESIGN.md §6 makes it a provider seam for exactly that reason.

**This used to be regexes, and regexes cannot do this job.** The old gate asked "does
this sentence look like a checkable factual claim", with a second pass asking "does it
look like someone pushing back" — word lists for `no`, `but`, `actually`, `that's not
right`. Both fail in both directions, and they fail on precisely the cases that matter:

- Half of real disagreement carries none of those words. "Enterprise is fine." followed
  by "Enterprise is where we're bleeding." is a flat contradiction with no negation, no
  contrast marker, and nothing for a pattern to match.
- Half the utterances that do carry them are not disagreement. "No, yeah, exactly" is
  agreement. "But anyway" is a topic change.
- A contradiction is a property of *two statements*, and a regex only ever sees one. No
  amount of pattern-matching on a single sentence can find a conflict whose other half
  was three turns ago.

So the gate is now a model. It reads the recent window and answers the question we
actually care about. It is the cheap, fast model — the expensive one still only runs on
what survives — but the ordering optimization is the only thing kept from the old design.

The one thing left before the model is a noise guard: length, and pure back-channel.
That is not contradiction detection, it is not paying an API call to think about the
word "yeah".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..providers.llm import LLMError, get_llm_provider
from .prompts import SCAN_SCHEMA, SCAN_SYSTEM, scan_user_prompt

log = logging.getLogger(__name__)

MIN_WORDS = 2
"""Below this there is no statement to conflict with anything."""

_BACKCHANNEL = frozenset(
    """
    yeah yes yep yup no nope ok okay sure right exactly totally agreed cool nice great
    thanks thank hello hi hey bye mhm uhhuh sounds good perfect got it makes sense fine
    absolutely definitely indeed alright uh um so well
    """.split()
)


def is_noise(text: str) -> bool:
    """Pure filler, with no proposition in it. The only free rejection left.

    Deliberately narrow. Anything that is not *entirely* back-channel goes to the model,
    because "no it isn't" is back-channel words carrying a contradiction and the whole
    point of this rewrite is to stop guessing about that from the surface form.
    """
    words = text.split()
    if len(words) < MIN_WORDS:
        return True
    return all(word.strip(".,!?;:").casefold() in _BACKCHANNEL for word in words)


@dataclass(frozen=True)
class ScanResult:
    """Whether this window earns the expensive call."""

    worth_checking: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.worth_checking


async def scan_for_conflict(*, transcript: str, latest: str) -> ScanResult:
    """Ask the cheap model whether this exchange might hold a contradiction.

    Fails **open**. A scan that errors, or a provider that is not configured, sends the
    window through to the real reasoning call rather than dropping it. A gate that fails
    closed silences the entire feature and does it invisibly, which is exactly the
    failure this pipeline has already been through once.
    """
    if is_noise(latest):
        return ScanResult(False, "back-channel")

    from ..store import store

    if not store.settings.ambient_scan:
        # The scan is skippable on purpose. If contradictions are being missed and it is
        # not obvious why, turning this off removes a whole stage from the diagnosis and
        # sends every non-noise utterance to the full reasoner.
        return ScanResult(True, "scan disabled; reasoning on everything")

    provider = get_llm_provider()
    if provider is None:
        return ScanResult(True, "no provider for the scan")

    try:
        result = await provider.complete_json(
            system=SCAN_SYSTEM,
            user=scan_user_prompt(transcript=transcript, latest=latest),
            schema=SCAN_SCHEMA,
            effort="low",
            max_tokens=1024,
            fast=True,
        )
    except LLMError as exc:
        log.warning("conflict scan failed (%s); reasoning anyway", exc)
        return ScanResult(True, "scan unavailable")

    return ScanResult(
        bool(result.get("worth_checking", True)),
        str(result.get("reason", "")).strip(),
    )
