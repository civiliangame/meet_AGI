"""Post-meeting review — sources, transcript search, and session housekeeping.

The dashboard is a place for a human to check, after the fact, what Meet AGI said and
what it read before saying it. These endpoints serve that.

Mounted under `/api/meetings` alongside `meetings.py` rather than in it. Two routers can
share a prefix, and keeping them separate lets the live-meeting and review surfaces be
edited independently — the two are owned by different workstreams right now. To a client
there is one `/api/meetings` resource; the split is invisible.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import archive
from ..errors import not_found
from ..schemas import (
    Ack,
    Interjection,
    Meeting,
    MeetingBundle,
    CitedDocument,
    Page,
    SourceQuote,
    TranscriptSegment,
)
from ..store import paginate, refresh_meeting_stats, store

router = APIRouter(prefix="/meetings", tags=["review"])


def _require(meeting_id: str) -> Meeting:
    meeting = store.meetings.get(meeting_id)
    if meeting is None:
        raise not_found("Meeting", meeting_id)
    return meeting


def _finals(meeting_id: str) -> list[TranscriptSegment]:
    return [s for s in store.segments_for(meeting_id) if s.is_final]


def _collect_sources(meeting_id: str) -> list[CitedDocument]:
    """Aggregate every citation in this session by the document it came from.

    Ordered by citation count, so the documents Meet AGI leaned on hardest are first —
    that is the order a human auditing the session wants to read them in.
    """
    by_document: dict[str, CitedDocument] = {}
    # (document, passage) -> the quote entry, so the same line cited by two claims is
    # one piece of evidence with two backlinks rather than a repeated block.
    by_passage: dict[tuple[str, str], SourceQuote] = {}

    for interjection in store.interjections_for(meeting_id):
        for citation in interjection.citations:
            entry = by_document.get(citation.document_id)
            if entry is None:
                entry = CitedDocument(
                    document_id=citation.document_id,
                    filename=citation.filename,
                    citation_count=0,
                    interjection_ids=[],
                    quotes=[],
                )
                by_document[citation.document_id] = entry

            entry.citation_count += 1
            if interjection.id not in entry.interjection_ids:
                entry.interjection_ids.append(interjection.id)

            key = (citation.document_id, citation.quote)
            quote = by_passage.get(key)
            if quote is None:
                quote = SourceQuote(
                    interjection_ids=[],
                    chunk_id=citation.chunk_id,
                    page=citation.page,
                    quote=citation.quote,
                    relevance=citation.relevance,
                )
                by_passage[key] = quote
                entry.quotes.append(quote)
            # Keep the strongest relevance seen for a passage used more than once.
            quote.relevance = max(quote.relevance, citation.relevance)
            if interjection.id not in quote.interjection_ids:
                quote.interjection_ids.append(interjection.id)

    return sorted(by_document.values(), key=lambda s: s.citation_count, reverse=True)


@router.get(
    "/{meeting_id}/sources",
    response_model=Page[CitedDocument],
    summary="Documents cited during a session",
    description=(
        "What Meet AGI actually read before it spoke, aggregated from the citations on "
        "this session's interjections — not merely what was available to search. "
        "Ordered by citation count. This is the audit surface: it answers whether "
        "Meet AGI looked at the right thing."
    ),
)
def get_sources(meeting_id: str) -> Page[CitedDocument]:
    _require(meeting_id)
    return Page[CitedDocument](items=_collect_sources(meeting_id), next_cursor=None)


@router.get(
    "/{meeting_id}/bundle",
    response_model=MeetingBundle,
    summary="Everything a review page needs, in one request",
    description=(
        "Meeting, full finalized transcript, interjections, and aggregated sources. "
        "One call so a review page renders without waterfalling four requests.\n\n"
        "Snapshot only. Live views should use `WS /api/meetings/{id}/live` instead."
    ),
)
def get_bundle(meeting_id: str) -> MeetingBundle:
    meeting = refresh_meeting_stats(_require(meeting_id))
    return MeetingBundle(
        meeting=meeting,
        transcript=_finals(meeting_id),
        interjections=list(reversed(store.interjections_for(meeting_id))),
        sources=_collect_sources(meeting_id),
    )


@router.get(
    "/{meeting_id}/search",
    response_model=Page[TranscriptSegment],
    summary="Search a session transcript",
    description=(
        "Case-insensitive substring match over finalized transcript, optionally scoped "
        "to one speaker. Returns segments in chronological order."
    ),
)
def search_transcript(
    meeting_id: str,
    q: str = Query(default="", description="Substring to match. Empty returns everything."),
    person_id: str | None = Query(default=None, description="Restrict to one speaker."),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> Page[TranscriptSegment]:
    _require(meeting_id)
    items = _finals(meeting_id)
    if person_id:
        items = [s for s in items if s.person_id == person_id]
    if q:
        needle = q.casefold()
        items = [s for s in items if needle in s.text.casefold()]
    window, next_cursor = paginate(items, cursor, limit)
    return Page[TranscriptSegment](items=window, next_cursor=next_cursor)


@router.get(
    "/{meeting_id}/interjections/{interjection_id}",
    response_model=Interjection,
    summary="Get one interjection",
    description="For deep-linking a single claim from a shared review URL.",
)
def get_interjection(meeting_id: str, interjection_id: str) -> Interjection:
    _require(meeting_id)
    for item in store.interjections_for(meeting_id):
        if item.id == interjection_id:
            return item
    raise not_found("Interjection", interjection_id)


@router.delete(
    "/{meeting_id}",
    response_model=Ack,
    summary="Delete a session",
    description=(
        "Removes the session and its archived bundle from disk. Irreversible. "
        "A live session is stopped first."
    ),
)
def delete_meeting(meeting_id: str) -> Ack:
    _require(meeting_id)
    if task := store.harness_tasks.pop(meeting_id, None):
        task.cancel()
    store.meetings.pop(meeting_id, None)
    store.segments.pop(meeting_id, None)
    store.interjections.pop(meeting_id, None)
    archive.delete(meeting_id)
    return Ack()
