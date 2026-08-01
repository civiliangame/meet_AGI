"""Shared contract primitives.

Everything in `app/schemas/` is the source of truth for the frontend/backend contract.
FastAPI derives OpenAPI from these models; `scripts/gen-types.sh` derives TypeScript
from that OpenAPI. Never hand-write an API type in the frontend.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, WithJsonSchema

T = TypeVar("T")


def _iso_ms(value: datetime) -> str:
    """RFC 3339 UTC with exactly milliseconds.

    Pydantic's default datetime serialization emits microseconds when present and
    drops the fractional part entirely when it is zero. That inconsistency is
    annoying to parse on the frontend, so every timestamp in the contract goes
    through here and always looks the same: `2026-08-01T18:22:04.118Z`.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return f"{value.strftime('%Y-%m-%dT%H:%M:%S')}.{value.microsecond // 1000:03d}Z"


Ts = Annotated[
    datetime,
    PlainSerializer(_iso_ms, return_type=str),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
"""A timestamp. Always serialized RFC 3339 UTC with milliseconds."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Schema(BaseModel):
    """Base for every contract model.

    `populate_by_name` lets the backend accept either field names or aliases;
    `use_enum_values` keeps string enums as plain strings in JSON so the frontend
    sees `"connected"` rather than a nested representation.

    `json_schema_serialization_defaults_required` is the one that matters for the
    frontend. A Pydantic field with a default is not *required*, so by default it is
    omitted from `required` in the JSON Schema — and the generated TypeScript types it
    as `T | undefined`. But a response always contains it: `citations` is `[]`, never
    absent. Without this flag every consumer writes `?? []` against a case the server
    cannot produce, which is noise that also hides the genuinely optional fields.

    This affects the *serialization* schema only, which is what FastAPI uses for
    responses. Request bodies keep their optional fields optional.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        use_enum_values=True,
        json_schema_serialization_defaults_required=True,
    )


class Page(Schema, Generic[T]):
    """Cursor-paginated list.

    Cursors are opaque. `next_cursor` is null on the last page. Pass it back as
    `?cursor=` to continue. Do not construct cursors on the frontend.
    """

    items: list[T]
    next_cursor: str | None = None


class ErrorDetail(Schema):
    code: str = Field(description="Stable machine-readable code, e.g. `not_found`.")
    message: str = Field(description="Human-readable message. Safe to surface in UI.")
    detail: dict[str, object] | None = Field(
        default=None, description="Optional structured context, e.g. field validation errors."
    )


class ErrorResponse(Schema):
    """Every non-2xx response from `/api` has this shape."""

    error: ErrorDetail


class Ack(Schema):
    """Generic success envelope for endpoints with nothing meaningful to return."""

    ok: bool = True
