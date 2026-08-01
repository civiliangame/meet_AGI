"""Integration — external context sources.

Every integration is stubbed for the hackathon: `connect` flips status after a
simulated delay and sets a plausible `account_label`. No OAuth happens.

`is_stub` exists so the UI can be honest without hardcoding which providers are fake.
Render a "Demo" badge when it is true. When a connection becomes real the backend flips
the flag and the badge disappears with no frontend change.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .common import Schema, Ts


class IntegrationProvider(str, Enum):
    SLACK = "slack"
    GMAIL = "gmail"
    GDRIVE = "gdrive"
    NOTION = "notion"
    SALESFORCE = "salesforce"


class IntegrationStatus(str, Enum):
    AVAILABLE = "available"
    CONNECTED = "connected"
    ERROR = "error"


class IntegrationCapability(str, Enum):
    DOCUMENTS = "documents"
    MESSAGES = "messages"
    CALENDAR = "calendar"
    CRM = "crm"


class Integration(Schema):
    provider: IntegrationProvider
    display_name: str = Field(examples=["Slack"])
    status: IntegrationStatus = IntegrationStatus.AVAILABLE
    connected_at: Ts | None = None
    account_label: str | None = Field(
        default=None,
        description="What account this is connected as. Display-only.",
        examples=["acme.slack.com"],
    )
    capabilities: list[IntegrationCapability] = Field(default_factory=list)
    is_stub: bool = Field(
        default=True,
        description="True when the connection is simulated. Drives the 'Demo' badge.",
    )
    error: str | None = Field(default=None, description="Populated only when `status` is `error`.")
