"""The two reasoning calls, and the fixture fallback behind them.

`check_claim` backs the ambient loop, `answer_question` backs speech mode. Both do the
same three things: retrieve, ask Claude for a structured verdict, and turn the chunk ids
it cites back into real `Citation` objects the frontend can render.

Citations are resolved against the chunks that were actually retrieved, never against
the model's free text. A model that cites a chunk id it was not shown gets that citation
dropped rather than a fabricated document link appearing in the dashboard.

When no API key is configured both functions fall back to the fixture's canned output.
That is what keeps the harness demo working offline — and it is honest about it, because
the resulting interjection is flagged in its `body_md`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..knowledge import Chunk, get_knowledge_base
from ..providers.llm import LLMError, LLMRefusal, get_llm_provider
from ..schemas import Citation
from .prompts import (
    AMBIENT_SCHEMA,
    AMBIENT_SYSTEM,
    ANSWER_SCHEMA,
    ANSWER_SYSTEM,
    ambient_user_prompt,
    answer_user_prompt,
)

log = logging.getLogger(__name__)

TOP_K = 6
"""Chunks passed to the reasoning call. Enough to hold both sides of a disagreement."""


@dataclass
class Verdict:
    """The ambient loop's conclusion about one utterance."""

    kind: str
    """contradiction | none. Nothing else interjects — DESIGN.md §4."""
    confidence: float
    topic: str
    """Short noun phrase naming what triggered this. Renders in the chat prefix."""
    headline: str
    chat_alert: str
    body_md: str
    statement_a: str = ""
    """The earlier statement, verbatim. Half of the pair that cannot both be true."""
    statement_b: str = ""
    """The claim under review, verbatim. The other half."""
    citations: list[Citation] = field(default_factory=list)

    @property
    def is_flag(self) -> bool:
        """Whether this interjects.

        Both statements are load-bearing, not decoration. A model that flags a
        contradiction but cannot quote the two conflicting statements has reasoned its
        way to a conclusion rather than read one off the transcript, and that is the
        exact failure mode that produces an interjection nobody in the room agrees with.
        """
        return (
            self.kind == "contradiction"
            and bool(self.statement_a.strip())
            and bool(self.statement_b.strip())
        )


@dataclass
class Answer:
    """Speech mode's response to a direct question."""

    spoken: str
    topic: str
    """Short noun phrase naming what was asked about. Renders in the chat prefix."""
    chat_alert: str
    headline: str
    body_md: str
    confidence: float
    citations: list[Citation] = field(default_factory=list)


NO_FLAG = Verdict(kind="none", confidence=0.0, topic="", headline="", chat_alert="", body_md="")


def _citations(chunks: list[Chunk], chunk_ids: list[str], quotes: list[str]) -> list[Citation]:
    """Resolve cited chunk ids against what was actually retrieved."""
    by_id = {chunk.id: chunk for chunk in chunks}
    citations: list[Citation] = []
    for index, chunk_id in enumerate(chunk_ids):
        chunk = by_id.get(chunk_id)
        if chunk is None:
            log.debug("model cited unknown chunk %s; dropping", chunk_id)
            continue
        quote = quotes[index] if index < len(quotes) else None
        # Relevance decays down the list: the model returns what it relied on most first.
        citations.append(chunk.as_citation(relevance=max(0.95 - 0.1 * index, 0.4), quote=quote))
    return citations


async def check_claim(*, claim: str, speaker: str, transcript: str) -> Verdict:
    """Does this claim contradict the documents, or something already said?

    Contradictions only. A claim the documents merely add colour to is not something
    Kindred says anything about.
    """
    provider = get_llm_provider()
    if provider is None:
        return NO_FLAG

    documents, chunks = get_knowledge_base().context_for(claim, top_k=TOP_K)

    try:
        result = await provider.complete_json(
            system=AMBIENT_SYSTEM,
            user=ambient_user_prompt(
                claim=claim, speaker=speaker, transcript=transcript, documents=documents
            ),
            schema=AMBIENT_SCHEMA,
            # Medium, not low: judging whether two figures genuinely conflict or merely
            # measure different things is the call this whole loop lives or dies on.
            effort="medium",
            max_tokens=8000,
        )
    except LLMRefusal as exc:
        log.warning("ambient check refused: %s", exc)
        return NO_FLAG
    except LLMError as exc:
        log.warning("ambient check failed: %s", exc)
        return NO_FLAG

    kind = str(result.get("verdict", "none"))
    if kind != "contradiction":
        return NO_FLAG

    verdict = Verdict(
        kind=kind,
        confidence=float(result.get("confidence", 0.0)),
        topic=str(result.get("topic", "")).strip(),
        headline=str(result.get("headline", "")).strip(),
        chat_alert=str(result.get("chat_alert", "")).strip(),
        body_md=str(result.get("body_md", "")).strip(),
        statement_a=str(result.get("statement_a", "")).strip(),
        statement_b=str(result.get("statement_b", "")).strip(),
        citations=_citations(
            chunks, list(result.get("chunk_ids", [])), list(result.get("quotes", []))
        ),
    )
    if not verdict.is_flag:
        log.info(
            "contradiction claimed without both statements; dropping (a=%r, b=%r)",
            verdict.statement_a[:60],
            verdict.statement_b[:60],
        )
        return NO_FLAG
    return verdict


async def answer_question(*, question: str, asker: str, transcript: str) -> Answer | None:
    """Answer a question asked out loud. None when reasoning is unavailable."""
    provider = get_llm_provider()
    if provider is None:
        return None

    # Retrieve against the question plus a little transcript: questions in meetings lean
    # on pronouns ("what does it say about that?") and the query is often too thin alone.
    query = f"{question}\n{transcript[-600:]}"
    documents, chunks = get_knowledge_base().context_for(query, top_k=TOP_K)

    try:
        result = await provider.complete_json(
            system=ANSWER_SYSTEM,
            user=answer_user_prompt(
                question=question, asker=asker, transcript=transcript, documents=documents
            ),
            schema=ANSWER_SCHEMA,
            # Low: someone is standing there waiting for audio. DESIGN.md §7 budgets
            # ~600ms for this call, and the answer is a lookup, not an analysis.
            effort="low",
            max_tokens=4000,
        )
    except LLMRefusal as exc:
        log.warning("answer refused: %s", exc)
        return None
    except LLMError as exc:
        log.warning("answer failed: %s", exc)
        return None

    spoken = str(result.get("spoken", "")).strip()
    if not spoken:
        return None

    return Answer(
        spoken=spoken,
        topic=str(result.get("topic", "")).strip(),
        chat_alert=str(result.get("chat_alert", "")).strip() or spoken,
        headline=str(result.get("headline", "")).strip() or spoken[:100],
        body_md=str(result.get("body_md", "")).strip() or spoken,
        confidence=float(result.get("confidence", 0.5)),
        citations=_citations(
            chunks, list(result.get("chunk_ids", [])), list(result.get("quotes", []))
        ),
    )
