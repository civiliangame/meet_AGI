"""Process configuration, read from the environment (and the repo-root `.env`).

Everything optional has a default that keeps the app importable without credentials —
the frontend, the schemas, and the fixture harness must all work on a laptop with no
Recall key. Only the code paths that actually talk to Recall check for the key, and
they fail with a clear message rather than a `None` deep in an HTTP call.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
ASSET_AUDIO_DIR = Path(__file__).resolve().parent / "assets" / "audio"
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
"""Plain-text corpus Kindred reasons over. One `.txt` per document."""


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        # Both, so it works whether uvicorn is started from the repo root or backend/.
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str | None = Field(
        default=None,
        description=(
            "Reasoning for both loops. Without it the pipeline falls back to canned "
            "fixture output so the harness demo still runs offline."
        ),
    )
    anthropic_model: str = "claude-opus-5"
    anthropic_fast_model: str = Field(
        default="claude-haiku-4-5",
        description="Used for triage — the highest-QPS call in the system. See DESIGN.md §6.",
    )

    inworld_api_key: str | None = None
    inworld_voice_id: str = Field(
        default="Dennis",
        description="Inworld voice catalogue id. `GET https://api.inworld.ai/tts/v1/voices` lists them.",
    )
    inworld_model_id: str = "inworld-tts-2"
    inworld_base_url: str = "https://api.inworld.ai"

    recall_api_key: str | None = None
    recall_region: str = Field(
        default="us-west-2",
        description=(
            "Recall API keys are region-scoped: a key from another region returns 401 on "
            "every call. If auth fails, check this before checking the key."
        ),
    )
    recall_timeout_seconds: float = 30.0

    bot_name: str = "Kindred"
    """Display name in the participant list. Google Meet shows this to everyone."""

    voice_provider: str = Field(
        default="auto",
        description=(
            "`auto` uses Inworld when INWORLD_API_KEY is set and falls back to the sample "
            "clips otherwise. Force one with `inworld` or `sample`."
        ),
    )

    speech_tail_padding_ms: int = Field(
        default=400,
        description=(
            "Extra hold after a clip's own duration before Kindred is considered idle. "
            "Recall buffers and mixes the audio, so playback finishes slightly after the "
            "POST returns; without padding, back-to-back clips clip each other's tails."
        ),
    )

    @property
    def recall_base_url(self) -> str:
        return f"https://{self.recall_region}.recall.ai/api/v1"


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return AppConfig()
