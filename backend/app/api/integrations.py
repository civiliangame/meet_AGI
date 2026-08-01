"""Integrations — stubbed for the hackathon.

`connect` waits a beat and flips status. No OAuth happens. The endpoint shape is the
real one, so wiring a genuine provider later touches only this module.

The frontend should key its "Demo" badge off `is_stub` rather than hardcoding a list of
fake providers. When a connection becomes real the backend flips the flag and the badge
disappears with no frontend change.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from ..errors import not_found
from ..schemas import (
    Integration,
    IntegrationProvider,
    IntegrationStatus,
    Page,
    utcnow,
)
from ..store import store

router = APIRouter(prefix="/integrations", tags=["integrations"])

_SIMULATED_CONNECT_SECONDS = 1.2

_ACCOUNT_LABELS: dict[IntegrationProvider, str] = {
    IntegrationProvider.SLACK: "acme.slack.com",
    IntegrationProvider.GMAIL: "sarah@acme.com",
    IntegrationProvider.GDRIVE: "drive.google.com/acme",
    IntegrationProvider.NOTION: "Acme Corp workspace",
    IntegrationProvider.SALESFORCE: "acme.my.salesforce.com",
}


@router.get("", response_model=Page[Integration], summary="List integrations")
def list_integrations() -> Page[Integration]:
    return Page[Integration](items=list(store.integrations.values()), next_cursor=None)


@router.post(
    "/{provider}/connect",
    response_model=Integration,
    summary="Connect an integration",
    description=(
        "Simulated for the hackathon: waits ~1.2s, then flips `status` to `connected` "
        "and fills in a plausible `account_label`. No OAuth is performed."
    ),
)
async def connect_integration(provider: IntegrationProvider) -> Integration:
    existing = store.integrations.get(provider.value)
    if existing is None:
        raise not_found("Integration", provider.value)
    await asyncio.sleep(_SIMULATED_CONNECT_SECONDS)
    updated = existing.model_copy(
        update={
            "status": IntegrationStatus.CONNECTED,
            "connected_at": utcnow(),
            "account_label": _ACCOUNT_LABELS.get(provider),
            "error": None,
        }
    )
    store.integrations[provider.value] = updated
    return updated


@router.delete("/{provider}", response_model=Integration, summary="Disconnect an integration")
def disconnect_integration(provider: IntegrationProvider) -> Integration:
    existing = store.integrations.get(provider.value)
    if existing is None:
        raise not_found("Integration", provider.value)
    updated = existing.model_copy(
        update={
            "status": IntegrationStatus.AVAILABLE,
            "connected_at": None,
            "account_label": None,
            "error": None,
        }
    )
    store.integrations[provider.value] = updated
    return updated
