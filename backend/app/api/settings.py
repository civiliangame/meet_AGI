"""Agent settings — a singleton.

Nested objects replace wholesale on PATCH. Send the complete sub-object (e.g. all three
`interjection` fields), not just the key you changed. This keeps the merge semantics
obvious rather than deep and surprising.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..schemas import Settings, SettingsUpdate
from ..store import store

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=Settings, summary="Get settings")
def get_settings() -> Settings:
    return store.settings


@router.patch(
    "",
    response_model=Settings,
    summary="Update settings",
    description="Partial update. Omitted keys are unchanged; nested objects replace wholesale.",
)
def update_settings(payload: SettingsUpdate) -> Settings:
    updates = payload.model_dump(exclude_unset=True)
    store.settings = store.settings.model_copy(update=updates)
    return store.settings
