"""HTTP and WebSocket routers.

Nothing in here reaches past `app.store` for data, so replacing the in-memory store
with Postgres in Milestone 3 does not touch these modules.
"""

from fastapi import APIRouter

from . import (
    dev,
    documents,
    integrations,
    interjections,
    live,
    meetings,
    people,
    schema_only,
    settings,
    speech,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(people.router)
api_router.include_router(documents.router)
api_router.include_router(integrations.router)
api_router.include_router(settings.router)
api_router.include_router(meetings.router)
api_router.include_router(speech.router)
api_router.include_router(interjections.router)
api_router.include_router(dev.router)
api_router.include_router(schema_only.router)

# WebSockets last: they carry no prefix of their own and mount directly under /api.
api_router.include_router(live.router)

__all__ = ["api_router"]
