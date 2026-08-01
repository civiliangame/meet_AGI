"""Document upload and management.

Uploads return immediately with `status: "pending"` and advance through the pipeline in
the background. Milestone 0 simulates that progression on a timer so the frontend can
build and test the status-chip transitions; Milestone 3 replaces the simulation with
real parsing, chunking, and embedding.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, File, Form, Query, UploadFile

from ..bus import bus
from ..errors import not_found
from ..schemas import (
    Document,
    DocumentSource,
    DocumentStatus,
    DocumentUpdate,
    Page,
    utcnow,
)
from ..store import paginate, store

router = APIRouter(prefix="/documents", tags=["documents"])

_SIMULATED_STAGE_SECONDS = 1.4


async def _simulate_ingestion(document_id: str) -> None:
    """Walk a document pending -> parsing -> embedding -> ready.

    Publishes `document.status_changed` on the global channel at each step, which is
    exactly what the real pipeline will do. The frontend cannot tell the difference,
    so status-chip work done now survives Milestone 3 unchanged.
    """
    for stage, chunk_count in (
        (DocumentStatus.PARSING, 0),
        (DocumentStatus.EMBEDDING, 0),
        (DocumentStatus.READY, 42),
    ):
        await asyncio.sleep(_SIMULATED_STAGE_SECONDS)
        document = store.documents.get(document_id)
        if document is None:
            return
        updated = document.model_copy(update={"status": stage, "chunk_count": chunk_count})
        store.documents[document_id] = updated
        bus.publish_global("document.status_changed", updated)


@router.get("", response_model=Page[Document], summary="List documents")
def list_documents(
    status: DocumentStatus | None = Query(default=None),
    tag: str | None = Query(default=None),
    source: DocumentSource | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> Page[Document]:
    items = sorted(store.documents.values(), key=lambda d: d.created_at, reverse=True)
    if status is not None:
        items = [d for d in items if d.status == status]
    if source is not None:
        items = [d for d in items if d.source == source]
    if tag is not None:
        items = [d for d in items if tag in d.tags]
    window, next_cursor = paginate(items, cursor, limit)
    return Page[Document](items=window, next_cursor=next_cursor)


@router.post(
    "",
    response_model=Page[Document],
    status_code=201,
    summary="Upload documents",
    description=(
        "Multipart upload. Accepts multiple files in one request. Returns immediately "
        "with `status: pending`; watch `document.status_changed` on the global socket, "
        "or poll this endpoint."
    ),
)
async def upload_documents(
    files: list[UploadFile] = File(...),
    tags: list[str] = Form(default=[]),
) -> Page[Document]:
    created: list[Document] = []
    for upload in files:
        payload = await upload.read()
        document = Document(
            id=store.new_document_id(),
            filename=upload.filename or "untitled",
            mime_type=upload.content_type or "application/octet-stream",
            size_bytes=len(payload),
            source=DocumentSource.UPLOAD,
            status=DocumentStatus.PENDING,
            chunk_count=0,
            tags=list(tags),
            created_at=utcnow(),
        )
        store.documents[document.id] = document
        created.append(document)
        # Fire-and-forget: the task holds no reference to the request.
        asyncio.create_task(_simulate_ingestion(document.id))
    return Page[Document](items=created, next_cursor=None)


@router.get("/{document_id}", response_model=Document, summary="Get a document")
def get_document(document_id: str) -> Document:
    document = store.documents.get(document_id)
    if document is None:
        raise not_found("Document", document_id)
    return document


@router.patch("/{document_id}", response_model=Document, summary="Update document tags")
def update_document(document_id: str, payload: DocumentUpdate) -> Document:
    document = store.documents.get(document_id)
    if document is None:
        raise not_found("Document", document_id)
    updates = payload.model_dump(exclude_unset=True)
    updated = document.model_copy(update=updates)
    store.documents[document_id] = updated
    return updated


@router.delete("/{document_id}", response_model=Document, summary="Delete a document")
def delete_document(document_id: str) -> Document:
    document = store.documents.pop(document_id, None)
    if document is None:
        raise not_found("Document", document_id)
    return document
