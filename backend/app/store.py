"""In-memory store, seeded with demo data.

Milestone 0 deliberately has no database. Your partner clones the repo, installs deps,
runs one command, and gets a working API with plausible data — no Postgres, no Docker,
no API keys. Every read path the frontend needs is real; persistence is not.

Milestone 3 replaces this with SQLAlchemy + pgvector behind the same accessors. Nothing
in `app/api/` reaches past this module, so that swap does not touch the routers.
"""

from __future__ import annotations

import asyncio
from typing import TypeVar

from .ids import (
    PREFIX_CHUNK,
    PREFIX_DOCUMENT,
    PREFIX_INTERJECTION,
    PREFIX_MEETING,
    PREFIX_PERSON,
    new_id,
)
from .schemas import (
    Citation,
    Document,
    DocumentSource,
    DocumentStatus,
    Integration,
    IntegrationCapability,
    IntegrationProvider,
    IntegrationStatus,
    Interjection,
    InterjectionKind,
    InterjectionStatus,
    InterjectionTrigger,
    Meeting,
    Person,
    Settings,
    TranscriptSegment,
    utcnow,
)

T = TypeVar("T")

# Stable ids for seeded records so fixtures can reference them by constant.
SARAH_ID = f"{PREFIX_PERSON}_01J8XK2M3N4P5Q6R7S8T9V"
MARCUS_ID = f"{PREFIX_PERSON}_01J8XK2M3N4P5Q6R7S8TAW"
PRIYA_ID = f"{PREFIX_PERSON}_01J8XK2M3N4P5Q6R7S8TBX"
DECK_DOC_ID = f"{PREFIX_DOCUMENT}_01J8XK4A5B6C7D8E9F0G1H"
NOTES_DOC_ID = f"{PREFIX_DOCUMENT}_01J8XK4A5B6C7D8E9F0G2J"
CHURN_DOC_ID = f"{PREFIX_DOCUMENT}_01J8XK4A5B6C7D8E9F0G3K"


_INTEGRATION_CATALOG: list[tuple[IntegrationProvider, str, list[IntegrationCapability]]] = [
    (
        IntegrationProvider.SLACK,
        "Slack",
        [IntegrationCapability.DOCUMENTS, IntegrationCapability.MESSAGES],
    ),
    (
        IntegrationProvider.GMAIL,
        "Gmail",
        [IntegrationCapability.DOCUMENTS, IntegrationCapability.MESSAGES],
    ),
    (
        IntegrationProvider.GDRIVE,
        "Google Drive",
        [IntegrationCapability.DOCUMENTS],
    ),
    (
        IntegrationProvider.NOTION,
        "Notion",
        [IntegrationCapability.DOCUMENTS],
    ),
    (
        IntegrationProvider.SALESFORCE,
        "Salesforce",
        [IntegrationCapability.CRM],
    ),
]


class Store:
    def __init__(self) -> None:
        self.people: dict[str, Person] = {}
        self.documents: dict[str, Document] = {}
        self.integrations: dict[str, Integration] = {}
        self.meetings: dict[str, Meeting] = {}
        self.segments: dict[str, list[TranscriptSegment]] = {}
        self.interjections: dict[str, list[Interjection]] = {}
        self.settings = Settings()
        self.harness_tasks: dict[str, asyncio.Task[None]] = {}
        self.seed()

    # --- seeding -----------------------------------------------------------------

    def seed(self) -> None:
        """Populate demo data.

        The seeded people and documents are the cast of the `q3_revenue_review`
        fixture. Keeping them consistent means the harness produces interjections
        with real citations pointing at real documents.
        """
        now = utcnow()

        self.people = {
            SARAH_ID: Person(
                id=SARAH_ID,
                display_name="Sarah Chen",
                aliases=["Sarah", "S. Chen"],
                role="VP Finance",
                org="Acme Corp",
                email="sarah@acme.com",
                bio="Owns the quarterly revenue model and presents the board deck.",
                created_at=now,
                updated_at=now,
            ),
            MARCUS_ID: Person(
                id=MARCUS_ID,
                display_name="Marcus Webb",
                aliases=["Marcus"],
                role="Head of Product",
                org="Acme Corp",
                email="marcus@acme.com",
                bio="Runs the new product line. Optimistic in forecasts, per past meetings.",
                created_at=now,
                updated_at=now,
            ),
            PRIYA_ID: Person(
                id=PRIYA_ID,
                display_name="Priya Raman",
                aliases=["Priya"],
                role="CEO",
                org="Acme Corp",
                email="priya@acme.com",
                bio="Chairs the quarterly review. Asks for sources.",
                created_at=now,
                updated_at=now,
            ),
        }

        self.documents = {
            DECK_DOC_ID: Document(
                id=DECK_DOC_ID,
                filename="Q3-board-deck.pdf",
                mime_type="application/pdf",
                size_bytes=284119,
                source=DocumentSource.UPLOAD,
                status=DocumentStatus.READY,
                chunk_count=84,
                tags=["finance", "q3"],
                created_at=now,
            ),
            NOTES_DOC_ID: Document(
                id=NOTES_DOC_ID,
                filename="product-line-review-notes.md",
                mime_type="text/markdown",
                size_bytes=14208,
                source=DocumentSource.NOTION,
                status=DocumentStatus.READY,
                chunk_count=22,
                tags=["product"],
                created_at=now,
            ),
            CHURN_DOC_ID: Document(
                id=CHURN_DOC_ID,
                filename="churn-analysis-august.xlsx",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                size_bytes=91744,
                source=DocumentSource.GDRIVE,
                status=DocumentStatus.EMBEDDING,
                chunk_count=0,
                tags=["finance", "churn"],
                created_at=now,
            ),
        }

        self.integrations = {
            provider.value: Integration(
                provider=provider,
                display_name=display_name,
                status=(
                    IntegrationStatus.CONNECTED
                    if provider in (IntegrationProvider.SLACK, IntegrationProvider.GDRIVE)
                    else IntegrationStatus.AVAILABLE
                ),
                connected_at=(
                    now
                    if provider in (IntegrationProvider.SLACK, IntegrationProvider.GDRIVE)
                    else None
                ),
                account_label=(
                    "acme.slack.com"
                    if provider is IntegrationProvider.SLACK
                    else "drive.google.com/acme" if provider is IntegrationProvider.GDRIVE else None
                ),
                capabilities=capabilities,
                is_stub=True,
            )
            for provider, display_name, capabilities in _INTEGRATION_CATALOG
        }

    def reset(self) -> None:
        """Wipe everything and re-seed. Backs `POST /api/dev/reset`."""
        for task in self.harness_tasks.values():
            task.cancel()
        self.harness_tasks.clear()
        self.meetings.clear()
        self.segments.clear()
        self.interjections.clear()
        self.settings = Settings()
        self.seed()

    # --- accessors ---------------------------------------------------------------

    def new_person_id(self) -> str:
        return new_id(PREFIX_PERSON)

    def new_document_id(self) -> str:
        return new_id(PREFIX_DOCUMENT)

    def new_meeting_id(self) -> str:
        return new_id(PREFIX_MEETING)

    def new_interjection_id(self) -> str:
        return new_id(PREFIX_INTERJECTION)

    def person_by_name(self, name: str) -> Person | None:
        """Match a platform display name to a known Person.

        Milestone 0 matches on display name and aliases, case-insensitively. Real
        identity resolution (calendar emails, voice prints) lands in `pipeline/identity.py`.
        """
        needle = name.strip().casefold()
        for person in self.people.values():
            if person.display_name.casefold() == needle:
                return person
            if any(alias.casefold() == needle for alias in person.aliases):
                return person
        return None

    def segments_for(self, meeting_id: str) -> list[TranscriptSegment]:
        return self.segments.setdefault(meeting_id, [])

    def interjections_for(self, meeting_id: str) -> list[Interjection]:
        return self.interjections.setdefault(meeting_id, [])

    def upsert_segment(self, segment: TranscriptSegment) -> None:
        """Insert, or replace a partial with the final that supersedes it.

        Partials and their final share an id, so this keeps exactly one entry per
        utterance and the frontend's transcript log stays stable.
        """
        segments = self.segments_for(segment.meeting_id)
        for index, existing in enumerate(segments):
            if existing.id == segment.id:
                segments[index] = segment
                return
        segments.append(segment)

    def find_interjection(self, interjection_id: str) -> tuple[str, int] | None:
        for meeting_id, items in self.interjections.items():
            for index, item in enumerate(items):
                if item.id == interjection_id:
                    return meeting_id, index
        return None

    # --- demo helpers ------------------------------------------------------------

    def demo_citation(self) -> Citation:
        return Citation(
            document_id=DECK_DOC_ID,
            filename="Q3-board-deck.pdf",
            chunk_id=f"{PREFIX_CHUNK}_01J8XK5B6C7D8E9F0G1H2J",
            page=14,
            quote="New Product Line: $1.42M (-12.1% MoM)",
            relevance=0.91,
        )

    def build_interjection(
        self,
        meeting_id: str,
        *,
        kind: InterjectionKind,
        status: InterjectionStatus,
        chat_alert: str,
        headline: str,
        body_md: str,
        confidence: float,
        trigger: InterjectionTrigger,
        citations: list[Citation] | None = None,
        spoken: bool = False,
    ) -> Interjection:
        now = utcnow()
        return Interjection(
            id=self.new_interjection_id(),
            meeting_id=meeting_id,
            kind=kind,
            status=status,
            trigger=trigger,
            chat_alert=chat_alert,
            headline=headline,
            body_md=body_md,
            confidence=confidence,
            citations=citations or [],
            spoken=spoken,
            created_at=now,
            posted_at=now if status is InterjectionStatus.POSTED else None,
        )


store = Store()


def paginate(items: list[T], cursor: str | None, limit: int) -> tuple[list[T], str | None]:
    """Offset pagination behind an opaque cursor.

    The cursor is a stringified offset today. It is documented as opaque so that
    switching to keyset pagination against Postgres later is not a contract change.
    """
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            start = 0
    window = items[start : start + limit]
    next_cursor = str(start + limit) if start + limit < len(items) else None
    return window, next_cursor
