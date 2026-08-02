"""Person — someone who attends meetings.

This is how Meet AGI knows *who* is talking and why they would say it. The `role` and
`bio` fields are fed to the reasoning model as context, so "VP Finance who owns the
quarterly revenue model" materially changes how a revenue claim gets evaluated.
"""

from __future__ import annotations

from pydantic import Field

from .common import Schema, Ts


class PersonBase(Schema):
    display_name: str = Field(min_length=1, max_length=120, examples=["Sarah Chen"])
    aliases: list[str] = Field(
        default_factory=list,
        description=(
            "Other names this person is called in transcripts. Used for speaker matching "
            "when the platform display name does not match the roster."
        ),
        examples=[["Sarah", "S. Chen"]],
    )
    role: str | None = Field(default=None, max_length=120, examples=["VP Finance"])
    org: str | None = Field(default=None, max_length=120, examples=["Acme Corp"])
    email: str | None = Field(default=None, max_length=254, examples=["sarah@acme.com"])
    bio: str | None = Field(
        default=None,
        max_length=2000,
        description="Free text. Passed to the reasoning model as speaker context.",
        examples=["Owns the quarterly revenue model. Presents the board deck."],
    )


class PersonCreate(PersonBase):
    pass


class PersonUpdate(Schema):
    """All fields optional. Omitted fields are left unchanged."""

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    aliases: list[str] | None = None
    role: str | None = Field(default=None, max_length=120)
    org: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    bio: str | None = Field(default=None, max_length=2000)


class Person(PersonBase):
    id: str = Field(examples=["prs_01J8XK2M3N4P5Q6R7S8T9V"])
    voice_sample_url: str | None = Field(
        default=None,
        description="Set once a voice sample is uploaded. Reserved for voice-print matching.",
    )
    created_at: Ts
    updated_at: Ts
