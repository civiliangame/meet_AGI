"""Settings — a singleton controlling how Meet AGI behaves.

The autonomy level is the trust decision at the centre of the product, so the
frontend should present it in plain language rather than as a bare enum:

- `silent`    Meet AGI never touches the meeting. Interjections appear in the
              dashboard only. The safe default and the safe demo.
- `propose`   Interjections wait for a human to approve before posting to chat.
- `auto_post` Interjections post to meeting chat immediately. The real product.

Speech mode is governed separately by `wake_word_enabled` — Meet AGI can be allowed to
answer out loud when asked while still being forbidden from commenting unprompted.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .common import Schema


class Autonomy(str, Enum):
    SILENT = "silent"
    PROPOSE = "propose"
    AUTO_POST = "auto_post"


class VoiceProviderName(str, Enum):
    INWORLD = "inworld"
    ELEVENLABS = "elevenlabs"
    SYSTEM = "system"


class PersonaProviderName(str, Enum):
    CHARACTERAI = "characterai"
    CLAUDE = "claude"


class TriageProviderName(str, Enum):
    TENSTORRENT = "tenstorrent"
    CLAUDE = "claude"
    HEURISTIC = "heuristic"


class PersonaTone(str, Enum):
    CONCISE_ANALYST = "concise_analyst"
    WARM_COLLEAGUE = "warm_colleague"
    BLUNT = "blunt"


class InterjectionPolicy(Schema):
    """Rate limiting is a feature, not an optimization.

    A copilot that will not shut up is worse than no copilot. These three knobs are
    the difference between "helpful" and "muted by the host in minute two", and they
    are exposed in the UI so they can be tuned live during a meeting.

    The defaults are tuned for *catching every contradiction*, which is what the
    ambient loop is now scoped to and nothing else. The old 90-second cooldown was
    written when the loop could also volunteer context, where the risk is chatter. A
    contradiction is rare and always worth hearing, and two of them thirty seconds
    apart are two separate things the room got wrong — swallowing the second one is
    the bug, not the rate limit working.
    """

    min_confidence: float = Field(
        default=0.6, ge=0.0, le=1.0, description="Below this, the interjection is discarded."
    )
    cooldown_seconds: int = Field(
        default=20, ge=0, le=3600, description="Minimum gap between interjections."
    )
    max_per_meeting: int = Field(default=30, ge=0, le=100)


class VoiceSettings(Schema):
    provider: VoiceProviderName = VoiceProviderName.INWORLD
    voice_id: str | None = Field(
        default=None,
        description=(
            "Provider-specific voice id, or null to use the one the server is "
            "configured with (`INWORLD_VOICE_ID`). Null is the default because a made-up "
            "id is not a placeholder here — the provider looks it up and 404s, and "
            "Meet AGI goes silent mid-meeting."
        ),
        examples=["Dennis"],
    )
    speaking_rate: float = Field(default=1.0, ge=0.5, le=2.0)


class PersonaSettings(Schema):
    provider: PersonaProviderName = PersonaProviderName.CHARACTERAI
    character_id: str | None = None
    tone: PersonaTone = PersonaTone.CONCISE_ANALYST


class TriageSettings(Schema):
    provider: TriageProviderName = TriageProviderName.HEURISTIC


SECURE_MEETING_DESCRIPTION = (
    "Route every reasoning call to Tenstorrent instead of the configured cloud provider "
    "(Gemini by default). The point is where the meeting's words are processed: "
    "Tenstorrent serves an open-weight Qwen on its own hardware, so a meeting that must "
    "not reach a third-party frontier API can still be reasoned over.\n\n"
    "Off by default — flipping it on is the deliberate act, and it costs latency and "
    "some judgment quality. Takes effect on the next reasoning call; no restart needed. "
    "If `TENSTORRENT_API_KEY` is unset the pipeline falls back to canned output rather "
    "than quietly sending the transcript to the cloud provider, which is the correct "
    "failure for a switch whose whole purpose is to keep data off it."
)


class Settings(Schema):
    wake_word: str = Field(
        default="Hey AGI",
        min_length=2,
        max_length=32,
        description=(
            "Spoken phrase that puts Meet AGI into speech mode. Matched against finalized "
            "transcript only, case- and punctuation-insensitively, with STT homophones "
            "("
            "`hey a g i`, `hey aji`) generated automatically."
        ),
    )
    wake_aliases: list[str] = Field(
        default_factory=lambda: ["Meet AGI"],
        description=(
            "Additional phrases that also wake the agent. `Meet AGI` is kept so it still "
            "answers to its own name and not only to the wake word."
        ),
    )
    wake_word_enabled: bool = True
    autonomy: Autonomy = Autonomy.AUTO_POST
    secure_meeting: bool = Field(default=False, description=SECURE_MEETING_DESCRIPTION)
    interjection: InterjectionPolicy = Field(default_factory=InterjectionPolicy)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    persona: PersonaSettings = Field(default_factory=PersonaSettings)
    triage: TriageSettings = Field(default_factory=TriageSettings)


class SettingsUpdate(Schema):
    """Partial update. Nested objects replace wholesale — send the complete sub-object."""

    wake_word: str | None = Field(default=None, min_length=2, max_length=32)
    wake_aliases: list[str] | None = None
    wake_word_enabled: bool | None = None
    autonomy: Autonomy | None = None
    secure_meeting: bool | None = Field(default=None, description=SECURE_MEETING_DESCRIPTION)
    interjection: InterjectionPolicy | None = None
    voice: VoiceSettings | None = None
    persona: PersonaSettings | None = None
    triage: TriageSettings | None = None
