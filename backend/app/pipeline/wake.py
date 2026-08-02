"""Wake-word detection, and the kill phrase that shuts Meet AGI up.

Matched against *finalized* transcript for waking. Partials revise as they arrive, and a
partial that briefly reads "hey a g..." fires a wake that the final then contradicts —
DESIGN.md §12 calls this the single likeliest thing to embarrass you on stage. The stop
phrase is the deliberate exception: see `StopDetector`.

Three problems this has to solve that a naive `in` check does not:

**STT mangles "AGI".** It is three letters, not a word, so real transcripts come back as
"hey a g i", "hey agi", "hey aji", "hey adji". The variant set below is generated from
the configured wake word plus a homophone list, so changing the wake word in settings
does not silently break matching.

**The homophone list is never complete.** Whisper and Deepgram invent spellings nobody
anticipated — "hey ajai", "hey hgi", "hey a jee". So after the exact pass there is a
fuzzy pass: strip the spaces out of a candidate window and compare it to the wake phrase
by edit distance. "heyaji" is one edit from "heyagi" and matches; "heygigi" is two and
does not. The fuzzy pass is held to a higher bar for what may follow it, because it is
the pass that can invent a wake out of an unrelated word.

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
    "agi": (
        "agi", "a g i", "a.g.i", "aji", "adji", "agee", "ayjee", "a gi", "ag i",
        "agie", "aggie", "augie", "auggie", "edgy", "a j i", "a jay", "ajai",
        "ajay", "h g i", "age i", "a gee", "eiji", "agy", "a chi",
    ),
    "kindred": ("kindred", "kindrid", "kindra", "kendrid", "kin dread"),
}

# Variants that are also ordinary words or names. They only ever match as part of a
# longer phrase ("hey aggie"), never on their own — "Augie said the numbers were fine"
# is not someone talking to the agent.
_NEVER_BARE = frozenset({"edgy", "aggie", "augie", "auggie", "ajay", "a jay", "agy", "a chi"})

_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")

# Words that mark what follows as addressed to someone: a question or an instruction.
# Deliberately broad — the cost of a word missing from this list is a wake that does not
# fire, which on stage is the failure everybody in the room can see.
_QUESTION_WORDS = frozenset(
    """what whats when where which who whom whose why how hows did do does is are was
    were can could should would will shall am have has had any anything remind tell show
    give find look search pull check confirm compare explain define list read recap
    repeat summarize summarise walk help got name state describe quote""".split()
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

    # A single-token wake word is also matched bare (just "agi", no "hey") — except for
    # the variants that are real words, which would wake on ordinary speech.
    if len(tokens) > 1:
        last = tokens[-1]
        combos.extend(
            variant
            for variant in _TOKEN_VARIANTS.get(last, (last,))
            if normalize(variant) not in _NEVER_BARE
        )

    return sorted({normalize(c) for c in combos if c}, key=len, reverse=True)


def _edit_distance(a: str, b: str, *, limit: int) -> int:
    """Levenshtein distance, abandoned once it is certainly over `limit`.

    The early exit is what makes it cheap enough to run per token window per utterance:
    almost every comparison is against something wildly different and bails on the first
    row.
    """
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    if a == b:
        return 0

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ca != cb),
                )
            )
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _fuzz_budget(target: str) -> int:
    """How many edits a target of this length may absorb and still be that target.

    One edit for a short phrase, two once it is long enough that two edits cannot turn
    it into a different word anybody actually says.
    """
    return 1 if len(target) < 9 else 2


@dataclass(frozen=True)
class WakeMatch:
    """A wake word was heard."""

    matched_text: str
    """The span that matched, for debugging false positives in the UI."""
    question: str
    """Whatever followed the wake word in the same utterance. May be empty."""
    fuzzy: bool = False
    """True when this came from the edit-distance pass rather than an exact variant."""

    @property
    def has_question(self) -> bool:
        """Whether the utterance carried its own question.

        Two thresholds, because "what's churn" is a real question at three normalized
        words and "yeah okay" is not: anything opening with a question or command word
        needs only two words, everything else needs three.
        """
        words = self.question.split()
        if not words:
            return False
        if words[0] in _QUESTION_WORDS:
            return len(words) >= 2
        return len(words) >= 3


class WakeDetector:
    """Matches a configured wake word and its STT variants."""

    def __init__(self, wake_word: str, aliases: list[str] | None = None) -> None:
        self._phrases: list[str] = []
        self._distinctive: set[str] = set()
        """Phrases nobody says by accident, so position alone is enough to wake on them.

        The property belongs to the *configured* phrase, not to the variant. "Hey AGI"
        and "Meet AGI" are two words each and distinctive; a one-word alias that is also
        an ordinary English word — "Kindred", the old name, was exactly this — is not.
        A variant has to inherit its phrase's caution even when STT splits it into two
        tokens (`kin dread` for `Kindred`), or "kindred spirits, the two of them" wakes
        the agent, which is the exact demo-day embarrassment DESIGN.md §12 is about.
        Both current phrases are distinctive, so the caution only bites on a one-word
        alias someone configures later — the code still has to handle it.
        """
        seen: set[str] = set()
        for phrase in [wake_word, *(aliases or [])]:
            distinctive = len(normalize(phrase).split()) > 1
            for variant in _variants(phrase):
                if variant not in seen:
                    seen.add(variant)
                    self._phrases.append(variant)
                if distinctive:
                    self._distinctive.add(variant)
        self._phrases.sort(key=len, reverse=True)

        # Targets for the fuzzy pass, spaces removed so tokenization differences
        # ("a g i" vs "agi") cost nothing. Short targets are excluded: at three
        # characters an edit budget of one matches half the language.
        distinctive_by_target: dict[str, bool] = {}
        for phrase in self._phrases:
            despaced = phrase.replace(" ", "")
            if len(despaced) < 4:
                continue
            distinctive_by_target[despaced] = distinctive_by_target.get(despaced, False) or (
                phrase in self._distinctive
            )
        self._fuzzy_targets: list[tuple[str, bool]] = sorted(
            distinctive_by_target.items(), key=lambda item: len(item[0]), reverse=True
        )
        self._max_window = max((len(p.split()) for p in self._phrases), default=1)

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

        return self._fuzzy_match(haystack)

    def _fuzzy_match(self, haystack: str) -> WakeMatch | None:
        """Edit-distance fallback for a spelling the homophone list has never seen.

        Only trusted at the start of an utterance or in front of a question word. A
        fuzzy hit buried mid-sentence with ordinary prose after it is far more likely to
        be a mangled English word than someone addressing the agent.
        """
        tokens = haystack.split()
        for index in range(len(tokens)):
            # Longest window first, same reason as longest variant first: it consumes
            # the whole wake phrase instead of leaving "g i" in front of the question.
            for size in range(min(self._max_window, len(tokens) - index), 0, -1):
                window = "".join(tokens[index : index + size])
                if len(window) < 4:
                    continue
                for target, distinctive in self._fuzzy_targets:
                    budget = _fuzz_budget(target)
                    if _edit_distance(window, target, limit=budget) > budget:
                        continue
                    remainder = " ".join(tokens[index + size :]).strip()
                    words = remainder.split()
                    addressed = bool(words) and words[0] in _QUESTION_WORDS
                    # Same rule as the exact pass, and for the same reason: only a
                    # distinctive phrase gets to wake on position alone. A fuzzy hit on
                    # a one-word alias opening a sentence that merely contains it does not.
                    if index == 0:
                        if not (distinctive or not words or addressed):
                            continue
                    elif not addressed:
                        continue
                    return WakeMatch(
                        matched_text=" ".join(tokens[index : index + size]),
                        question=remainder,
                        fuzzy=True,
                    )
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

    def _is_addressed(self, haystack: str, start: int, phrase: str) -> bool:
        """Whether this occurrence addresses the agent rather than mentioning it.

        Punctuation is gone by this point, so the comma in "Meet AGI, what does..." is not
        available as a signal. Two things stand in for it:

        - **How distinctive the phrase is.** "hey agi" and "meet agi" are not things
          anyone says by accident, so at the start of an utterance either is a wake on
          its own. A one-word alias that is also an ordinary English word gets no such
          credit — "kindred spirits, the two of them" must not wake the agent mid-demo.
        - **Question-shaped continuation.** Whatever the phrase, if what follows looks
          like a question or a command, it is being addressed to someone.
        """
        remainder = haystack[start + len(phrase) :].split()
        looks_addressed = bool(remainder) and remainder[0] in _QUESTION_WORDS

        if start == 0:
            return phrase in self._distinctive or not remainder or looks_addressed

        # Mid-utterance the bar is higher: the phrase has to be followed by a question,
        # which rules out "we should call it hey agi" and "talking to Meet AGI about this".
        return looks_addressed


# --- The kill phrase ------------------------------------------------------------------

# "stop", "stop talking", "shut up", "be quiet". Ordered longest-first inside the regex
# so "stop talking" wins over the bare "stop" and the log says what was actually heard.
_STOP_COMMAND = re.compile(
    r"\b("
    r"stop talking|stop speaking|stop it|stop|shut up|shush|hush|"
    r"be quiet|quiet|enough|zip it|cancel that|cancel|never mind|nevermind|"
    r"stand down|that s enough|thats enough"
    r")\b"
)

STOP_PROXIMITY_TOKENS = 4
"""How far from the agent's name the stop verb may sit and still count.

"AGI, stop talking" and "stop talking, AGI" both land inside this. "AGI said the deck
was wrong, so we should stop the rollout" does not — the verb is eight tokens away and
is about something else entirely.
"""


@dataclass(frozen=True)
class StopMatch:
    """Someone told Meet AGI to shut up."""

    matched_text: str
    """The stop verb that fired, for the log."""
    addressed_as: str
    """The name token it was attached to."""


class StopDetector:
    """Matches "AGI stop talking" and everything a person might say instead.

    Deliberately *not* held to the wake detector's finalized-transcript rule. This is a
    kill switch: it runs on partial transcript so it fires while the speaker is still
    saying it, and the worst a false positive can do is make Meet AGI stop talking —
    which is exactly what someone reaching for this phrase wants anyway.

    The rule is name plus verb, in either order, within a few tokens. Requiring the name
    is what keeps "okay, everyone stop talking over each other" from muting the agent.
    """

    def __init__(self, wake_word: str, aliases: list[str] | None = None) -> None:
        # Only the *name* tokens matter, not the full "hey agi" phrase — people drop the
        # "hey" when they are cutting you off.
        names: set[str] = set()
        for phrase in [wake_word, *(aliases or [])]:
            tokens = normalize(phrase).split()
            if not tokens:
                continue
            name = tokens[-1]
            for variant in _TOKEN_VARIANTS.get(name, (name,)):
                normalized = normalize(variant)
                if normalized and normalized not in _NEVER_BARE:
                    names.add(normalized)
        self._names = sorted(names, key=len, reverse=True)

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def match(self, text: str) -> StopMatch | None:
        haystack = normalize(text)
        if not haystack:
            return None

        command = _STOP_COMMAND.search(haystack)
        if command is None:
            return None

        tokens = haystack.split()
        verb_index = len(haystack[: command.start()].split())
        verb_span = len(command.group(1).split())

        for name in self._names:
            name_tokens = name.split()
            for index in range(len(tokens) - len(name_tokens) + 1):
                if tokens[index : index + len(name_tokens)] != name_tokens:
                    continue
                # Distance from whichever end of the name is nearer the verb.
                gap = min(
                    abs(verb_index - (index + len(name_tokens))),
                    abs(index - (verb_index + verb_span)),
                )
                if gap <= STOP_PROXIMITY_TOKENS:
                    return StopMatch(matched_text=command.group(1), addressed_as=name)
        return None


def build_detector() -> WakeDetector:
    """A detector for the current settings."""
    from ..store import store

    settings = store.settings
    return WakeDetector(settings.wake_word, list(settings.wake_aliases))


def build_stop_detector() -> StopDetector:
    """A kill-phrase detector for the current settings."""
    from ..store import store

    settings = store.settings
    return StopDetector(settings.wake_word, list(settings.wake_aliases))
