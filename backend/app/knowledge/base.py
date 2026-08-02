"""Loading, chunking, and retrieving the `.txt` corpus.

Retrieval is a keyword prefilter, not semantic search. It exists to keep the reasoning
prompt small enough to be fast, not to be the final word on relevance — Claude reads the
chunks that survive and decides what actually matters. Recall matters far more than
precision here: a chunk wrongly included costs a few hundred tokens, a chunk wrongly
excluded is a fact Meet AGI cannot see.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ..config import KNOWLEDGE_DIR
from ..ids import PREFIX_CHUNK, new_id
from ..schemas import Citation

log = logging.getLogger(__name__)

_HEADING = re.compile(r"^##\s+(.*)$", re.MULTILINE)
_PAGE_HINT = re.compile(r"\[p\.\s*(\d+)\]")
_WORD = re.compile(r"[a-z0-9][a-z0-9'%.\-]*")

_STOPWORDS = frozenset(
    """
    a about actually all also am an and any are as at be been being but by can cant could
    did do does doing dont down for from get go going got had has have he her here hers him
    his how i if in into is it its just know let like me mean more most much my no not now
    of off ok okay on one only or other our out over really right said say says see she so
    some than that the their them then there these they thing think this those to too up us
    very want was way we well were what when where which who why will with would yeah yes
    you your
    """.split()
)

_NUMERIC = re.compile(r"\d")


@dataclass(frozen=True)
class Chunk:
    """One retrievable passage: a `##` section of one document."""

    id: str
    document_id: str | None
    """The seeded `Document` this came from, when one matches by filename stem."""
    filename: str
    heading: str
    text: str
    page: int | None = None
    tokens: frozenset[str] = field(default_factory=frozenset)

    def as_citation(self, relevance: float, quote: str | None = None) -> Citation:
        return Citation(
            document_id=self.document_id or self.filename,
            filename=self.filename,
            chunk_id=self.id,
            page=self.page,
            quote=(quote or self.best_quote()),
            relevance=round(min(max(relevance, 0.0), 1.0), 2),
        )

    def best_quote(self, limit: int = 220) -> str:
        """The most quotable line: prefer one carrying a number, else the first line."""
        lines = [line.strip() for line in self.text.splitlines() if line.strip()]
        if not lines:
            return self.heading[:limit]
        numeric = [line for line in lines if _NUMERIC.search(line)]
        return (numeric[0] if numeric else lines[0])[:limit]

    def render(self) -> str:
        """How this chunk appears in a prompt."""
        page = f" (p.{self.page})" if self.page else ""
        return f"[{self.id}] {self.filename}{page} — {self.heading}\n{self.text}"


def _tokenize(text: str) -> set[str]:
    return {
        word
        for word in _WORD.findall(text.casefold())
        if len(word) > 2 and word not in _STOPWORDS
    }


class KnowledgeBase:
    """An in-memory corpus of chunked text documents."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self._chunks = chunks

    @property
    def chunks(self) -> list[Chunk]:
        return list(self._chunks)

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    @property
    def document_count(self) -> int:
        return len({chunk.filename for chunk in self._chunks})

    @property
    def filenames(self) -> list[str]:
        seen: list[str] = []
        for chunk in self._chunks:
            if chunk.filename not in seen:
                seen.append(chunk.filename)
        return seen

    def get(self, chunk_id: str) -> Chunk | None:
        return next((c for c in self._chunks if c.id == chunk_id), None)

    def retrieve(self, query: str, *, top_k: int = 6) -> list[tuple[Chunk, float]]:
        """Top-k chunks for a query, best first, each with a 0-1 score.

        Scoring is overlap of distinct query terms, with a modest bonus for numeric
        tokens — meetings argue about numbers, and "12.1%" appearing in both the
        utterance and a chunk is a much stronger signal than a shared common word.
        """
        terms = _tokenize(query)
        if not terms or not self._chunks:
            return []

        scored: list[tuple[Chunk, float]] = []
        for chunk in self._chunks:
            hits = terms & chunk.tokens
            if not hits:
                continue
            weight = sum(1.6 if _NUMERIC.search(term) else 1.0 for term in hits)
            scored.append((chunk, weight / len(terms)))

        scored.sort(key=lambda pair: (-pair[1], pair[0].id))
        return scored[:top_k]

    def context_for(self, query: str, *, top_k: int = 6) -> tuple[str, list[Chunk]]:
        """Retrieved chunks rendered for a prompt, plus the chunks themselves.

        Falls back to the head of the corpus when nothing matches. An unmatched query is
        usually a short or pronoun-heavy question ("what did we say about that?"), and
        showing Claude some corpus beats showing it none — it can always answer that the
        documents do not cover the question.
        """
        hits = self.retrieve(query, top_k=top_k)
        chunks = [chunk for chunk, _ in hits] or self._chunks[:top_k]
        return "\n\n".join(chunk.render() for chunk in chunks), chunks


def _load_document(path: Path, doc_ids: dict[str, str]) -> list[Chunk]:
    raw = path.read_text(encoding="utf-8")
    stem = path.stem
    document_id = doc_ids.get(stem.casefold())
    filename = _display_filename(stem, doc_ids)

    # Everything before the first `##` is the document preamble — keep it as a chunk so
    # definitional headers ("all figures are NET revenue") stay retrievable.
    sections: list[tuple[str, str]] = []
    matches = list(_HEADING.finditer(raw))
    preamble = raw[: matches[0].start()] if matches else raw
    if preamble.strip():
        sections.append(("Overview", preamble.strip()))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        body = raw[match.end() : end].strip()
        if body:
            sections.append((match.group(1).strip(), body))

    chunks: list[Chunk] = []
    for heading, body in sections:
        page_match = _PAGE_HINT.search(heading) or _PAGE_HINT.search(body)
        chunks.append(
            Chunk(
                id=new_id(PREFIX_CHUNK),
                document_id=document_id,
                filename=filename,
                heading=_PAGE_HINT.sub("", heading).strip(),
                text=body,
                page=int(page_match.group(1)) if page_match else None,
                tokens=frozenset(_tokenize(f"{heading}\n{body}")),
            )
        )
    return chunks


def _display_filename(stem: str, doc_ids: dict[str, str]) -> str:
    """Prefer the seeded Document's filename so the UI shows `.pdf`, not `.txt`."""
    from ..store import store

    document_id = doc_ids.get(stem.casefold())
    if document_id and (document := store.documents.get(document_id)):
        return document.filename
    return f"{stem}.txt"


def _seeded_document_ids() -> dict[str, str]:
    """Map filename stem → seeded `Document.id`, so citations resolve in the frontend."""
    from ..store import store

    return {
        Path(document.filename).stem.casefold(): document.id
        for document in store.documents.values()
    }


def load_knowledge_base(directory: Path | None = None) -> KnowledgeBase:
    directory = directory or KNOWLEDGE_DIR
    if not directory.exists():
        log.warning(
            "no knowledge directory at %s — Meet AGI has nothing to check claims against",
            directory,
        )
        return KnowledgeBase([])

    doc_ids = _seeded_document_ids()
    chunks: list[Chunk] = []
    for path in sorted(directory.glob("*.txt")):
        try:
            chunks.extend(_load_document(path, doc_ids))
        except OSError:  # one unreadable file must not empty the corpus
            log.exception("could not read %s", path)
    return KnowledgeBase(chunks)


@lru_cache(maxsize=1)
def get_knowledge_base() -> KnowledgeBase:
    return load_knowledge_base()


def reload_knowledge_base() -> KnowledgeBase:
    """Re-read the corpus from disk. Backs `POST /api/dev/reset` and the tests."""
    get_knowledge_base.cache_clear()
    return get_knowledge_base()
