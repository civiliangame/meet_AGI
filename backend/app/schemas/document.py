"""Document — the context Meet AGI checks claims against.

Uploads return immediately with `status: "pending"` and progress through the pipeline
asynchronously. The frontend should render the status chip and not assume a freshly
uploaded document is queryable.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .common import Schema, Ts


class DocumentSource(str, Enum):
    UPLOAD = "upload"
    SLACK = "slack"
    GMAIL = "gmail"
    GDRIVE = "gdrive"
    NOTION = "notion"


class DocumentStatus(str, Enum):
    PENDING = "pending"
    PARSING = "parsing"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"


class Document(Schema):
    id: str = Field(examples=["doc_01J8XK4A5B6C7D8E9F0G1H"])
    filename: str = Field(examples=["Q3-board-deck.pdf"])
    mime_type: str = Field(examples=["application/pdf"])
    size_bytes: int = Field(ge=0, examples=[284119])
    source: DocumentSource = DocumentSource.UPLOAD
    status: DocumentStatus = DocumentStatus.PENDING
    error: str | None = Field(
        default=None, description="Populated only when `status` is `failed`."
    )
    chunk_count: int = Field(
        default=0, ge=0, description="Retrievable chunks. Zero until `status` is `ready`."
    )
    tags: list[str] = Field(default_factory=list, examples=[["finance", "q3"]])
    created_at: Ts


class DocumentUpdate(Schema):
    tags: list[str] | None = None


class Citation(Schema):
    """A pointer into a document, attached to an interjection.

    `quote` is the exact retrieved span. The frontend should render it verbatim —
    it is the evidence, and paraphrasing it would undercut the whole feature.
    """

    document_id: str = Field(examples=["doc_01J8XK4A5B6C7D8E9F0G1H"])
    filename: str = Field(examples=["Q3-board-deck.pdf"])
    chunk_id: str = Field(examples=["chk_01J8XK5B6C7D8E9F0G1H2J"])
    page: int | None = Field(default=None, description="1-indexed. Null for non-paginated sources.")
    quote: str = Field(examples=["New Product Line: $1.42M (-12.1% MoM)"])
    relevance: float = Field(ge=0.0, le=1.0, examples=[0.91])
