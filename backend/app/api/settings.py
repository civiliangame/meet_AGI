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

    # Merge into a plain dict and re-validate, rather than `model_copy(update=...)`.
    # `model_copy` writes values in without validating: a nested object would be stored
    # as the raw dict it arrived as, so `settings.voice.voice_id` becomes an
    # AttributeError at the moment Meet AGI tries to speak. Re-validating also enforces
    # the field constraints — bounds on `min_confidence`, `speaking_rate` and the rest —
    # which `model_copy` skips entirely.
    merged = store.settings.model_dump()
    merged.update(updates)
    store.settings = Settings.model_validate(merged)
    return store.settings
