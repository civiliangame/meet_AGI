"""Schema-only endpoints.

WebSocket event types do not appear in OpenAPI, because OpenAPI has no concept of a
socket frame. Without help, `LiveEvent` would never reach the generated TypeScript and
the frontend would be forced to hand-write the event union — exactly the drift this
contract exists to prevent.

So we declare one unused REST endpoint whose response model is the event union. It
exists purely to pull `LiveEvent` and every payload type into the OpenAPI schema, and
therefore into `generated.ts`.

Calling it returns 501. Nothing should ever call it.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..errors import ApiError
from ..schemas import ClientMessage, LiveEvent

router = APIRouter(prefix="/_schema", tags=["_schema"])


@router.get(
    "/live-event",
    response_model=LiveEvent,
    summary="Schema anchor for WebSocket events (never call this)",
    description=(
        "Not a real endpoint. It exists so the `LiveEvent` discriminated union and all "
        "of its payload types land in the OpenAPI document and thus in the generated "
        "TypeScript client. Returns 501 if called.\n\n"
        "Import the generated `LiveEvent` type in the frontend and `switch` on "
        "`event.type` — always with a `default` branch, because new event types are "
        "added without announcement."
    ),
    responses={501: {"description": "Always. This endpoint is a schema anchor only."}},
)
def live_event_schema() -> LiveEvent:
    raise ApiError(
        501,
        "not_implemented",
        "This endpoint exists only to publish WebSocket event types into the OpenAPI schema.",
    )


@router.get(
    "/client-message",
    response_model=ClientMessage,
    summary="Schema anchor for client-to-server socket frames (never call this)",
    description="Not a real endpoint. See `/api/_schema/live-event`. Returns 501 if called.",
    responses={501: {"description": "Always. This endpoint is a schema anchor only."}},
)
def client_message_schema() -> ClientMessage:
    raise ApiError(
        501,
        "not_implemented",
        "This endpoint exists only to publish the client message type into the OpenAPI schema.",
    )
