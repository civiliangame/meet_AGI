"""Wake-word detection.

Matched against *finalized* transcript only. Partials revise as they arrive, and a
partial that briefly reads "hey a g..." fires a wake that the final then contradicts —
DESIGN.md §12 calls this the single likeliest thing to embarrass you on stage.

Two problems this has to solve that a naive `in` check does not:

**STT mangles "AGI".** It is three letters, not a word, so real transcripts come back as
"hey a g i", "hey agi", "hey aji", "hey adji". The variant set below is generated from
the configured wake word plus a short homophone list, so changing the wake word in
settings does not silently break matching.

**People say the wake word while talking *about* it.** "we should call it hey agi" must
not wake. The guard is positional: the phrase has to start the utterance, or be preceded
by a clause boundary — which is where someone addressing the agent mid-sentence
("hold on, hey AGI, what does the deck say") actually puts it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Homophones for tokens STT reliably mangles. Keyed by the token as written in settings.
_TOKEN_VARIANTS: dict[str, tuple[str, ...]] = {
    "agi": ("agi", "a g i", "a.g.i", "aji", "adji", "agee", "ayjee", "a gi"),
    "kindred": ("kindred", "kindrid", "kindra", "kendrid"),
}

_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")

_QUESTION_WORDS = frozenset(
    """what when where which who whom whose why how did do does is are was were can could
    should would will remind tell show give find summarize summarise check""".split()
)

# Ends of clauses. A wake phrase right after one is someone turning to address the agent.
_CLAUSE_BOUNDARY = re.compile(r"[.,;:!?—-]\s*$")


def normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", text.casefold())).strip()


def _variants(phrase: str) -> list[str]:
    """Every normalized spelling of a wake phrase, longest first.

    Longest first matters: "a g i" and "agi" both match "hey a g i", but only the longer
    one consumes the right number of characters, and the remainder is the question.
    """
    tokens = normalize(phrase).split()
    if not tokens:
        return []

    options: list[list[str]] = [
        list(_TOKEN_VARIANTS.get(token, (token,))) for token in tokens
    ]

    combos = [""]
    for choices in options:
        combos = [f"{prefix} {choice}".strip() for prefix in combos for choice in choices]

    # A single-token wake word is also matched bare (just "agi", no "hey").
    if len(tokens) > 1:
        last = tokens[-1]
        combos.extend(_TOKEN_VARIANTS.get(last, (last,)))

    return sorted({normalize(c) for c in combos if c}, key=len, reverse=True)


@dataclass(frozen=True)
class WakeMatch:
    """A wake word was heard."""

    matched_text: str
    """The span that matched, for debugging false positives in the UI."""
    question: str
    """Whatever followed the wake word in the same utterance. May be empty."""

    @property
    def has_question(self) -> bool:
        """Whether the utterance carried its own question.

        Three words is the threshold: "what about churn" is a question, "yeah" and
        "one sec" are the speaker still getting there.
        """
        return len(self.question.split()) >= 3


class WakeDetector:
    """Matches a configured wake word and its STT variants."""

    def __init__(self, wake_word: str, aliases: list[str] | None = None) -> None:
        self._phrases: list[str] = []
        seen: set[str] = set()
        for phrase in [wake_word, *(aliases or [])]:
            for variant in _variants(phrase):
                if variant not in seen:
                    seen.add(variant)
                    self._phrases.append(variant)
        self._phrases.sort(key=len, reverse=True)

    @property
    def phrases(self) -> list[str]:
        return list(self._phrases)

    def match(self, text: str) -> WakeMatch | None:
        """Find a wake word in a finalized utterance, or None."""
        haystack = normalize(text)
        if not haystack:
            return None

        for phrase in self._phrases:
            for start in self._occurrences(haystack, phrase):
                if not self._is_addressed(haystack, start, phrase):
                    continue
                remainder = haystack[start + len(phrase) :].strip()
                return WakeMatch(matched_text=phrase, question=remainder)
        return None

    @staticmethod
    def _occurrences(haystack: str, phrase: str) -> list[int]:
        """Whole-token occurrences of `phrase` in `haystack`."""
        found: list[int] = []
        start = haystack.find(phrase)
        while start != -1:
            before_ok = start == 0 or haystack[start - 1] == " "
            end = start + len(phrase)
            after_ok = end == len(haystack) or haystack[end] == " "
            if before_ok and after_ok:
                found.append(start)
            start = haystack.find(phrase, start + 1)
        return found

    @staticmethod
    def _is_addressed(haystack: str, start: int, phrase: str) -> bool:
        """Whether this occurrence addresses the agent rather than mentioning it.

        Punctuation is gone by this point, so the comma in "Kindred, what does..." is not
        available as a signal. Two things stand in for it:

        - **How distinctive the phrase is.** "hey agi" is not something anyone says by
          accident, so at the start of an utterance it is a wake on its own. A one-word
          alias like "Kindred" is also an ordinary English word, and "kindred spirits,
          the two of them" must not wake the agent mid-demo.
        - **Question-shaped continuation.** Whatever the phrase, if what follows looks
          like a question or a command, it is being addressed to someone.
        """
        remainder = haystack[start + len(phrase) :].split()
        looks_addressed = bool(remainder) and remainder[0] in _QUESTION_WORDS

        if start == 0:
            is_distinctive = " " in phrase
            return is_distinctive or not remainder or looks_addressed

        # Mid-utterance the bar is higher: the phrase has to be followed by a question,
        # which rules out "we should call it hey agi" and "talking to Kindred about this".
        return looks_addressed


def build_detector() -> WakeDetector:
    """A detector for the current settings."""
    from ..store import store

    settings = store.settings
    return WakeDetector(settings.wake_word, list(settings.wake_aliases))
