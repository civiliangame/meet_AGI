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
"""Plain-text corpus Meet AGI reasons over. One `.txt` per document."""


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        # Both, so it works whether uvicorn is started from the repo root or backend/.
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: str = Field(
        default="auto",
        description=(
            "Reasoning backend. `auto` prefers Gemini when GEMINI_API_KEY is set, then "
            "Claude, then Tenstorrent, then falls back to the fixture's canned output. "
            "Force one with `gemini`, `claude`, `tenstorrent`, or `none`."
        ),
    )

    gemini_api_key: str | None = Field(
        default=None,
        description=(
            "Reasoning for both loops. Must be the bare key — a value wrapped in quotes "
            "in `.env` is sent verbatim and 400s as an invalid key."
        ),
    )
    gemini_model: str = Field(
        default="gemini-3.5-flash-lite",
        description=(
            "There is no `gemini-3.6-flash-lite`; 3.5 is the newest *lite*. Raise to "
            "`gemini-3.6-flash` if contradiction judgment is weak. The floating alias "
            "`gemini-flash-lite-latest` also works, but a model that changes under a "
            "live demo is not worth the freshness."
        ),
    )
    gemini_fast_model: str = Field(
        default="gemini-3.5-flash-lite",
        description="Triage — the highest-QPS call in the system. See DESIGN.md §6.",
    )

    anthropic_api_key: str | None = Field(
        default=None,
        description="Alternative reasoning backend. Used when GEMINI_API_KEY is unset.",
    )
    anthropic_model: str = "claude-opus-5"
    anthropic_fast_model: str = Field(
        default="claude-haiku-4-5",
        description="Used for triage when Claude is the active provider.",
    )

    tenstorrent_api_key: str | None = Field(
        default=None,
        description=(
            "Qwen served on Tenstorrent hardware, through an OpenAI-compatible endpoint. "
            "Set `LLM_PROVIDER=tenstorrent` to route reasoning here instead of Gemini."
        ),
    )
    tenstorrent_base_url: str = Field(
        default="https://console.tenstorrent.com/v1",
        description="OpenAI-compatible base. `GET {base}/models` lists what a key can reach.",
    )
    tenstorrent_model: str = Field(
        default="Qwen/Qwen3-32B",
        description=(
            "The catalogue also serves `Qwen/Qwen3-VL-32B-Instruct`, which is the newer "
            "model and is deliberately not the default: it accepts `response_format` and "
            "then ignores it, answering HTTP 200 in whatever JSON shape it likes. Every "
            "call the pipeline makes is schema-constrained, so that is unusable. "
            "Qwen3-32B enforces the schema."
        ),
    )
    tenstorrent_fast_model: str = Field(
        default="Qwen/Qwen3-32B",
        description="Triage. The same model — the catalogue has no smaller Qwen.",
    )

    @property
    def resolved_llm_provider(self) -> str:
        """Which reasoning backend `auto` actually picks. `none` means canned output."""
        if self.llm_provider != "auto":
            return self.llm_provider
        if self.gemini_api_key:
            return "gemini"
        if self.anthropic_api_key:
            return "claude"
        # Last in the chain on purpose: a Tenstorrent key sitting in `.env` should not
        # silently take over from Gemini. Flipping LLM_PROVIDER is the deliberate act.
        if self.tenstorrent_api_key:
            return "tenstorrent"
        return "none"

    inworld_api_key: str | None = None
    inworld_voice_id: str = Field(
        default="Dennis",
        description="Inworld voice catalogue id. `GET https://api.inworld.ai/tts/v1/voices` lists them.",
    )
    inworld_model_id: str = "inworld-tts-2"
    inworld_base_url: str = "https://api.inworld.ai"

    public_base_url: str | None = Field(
        default=None,
        description=(
            "Publicly reachable https base URL for this backend — an ngrok or Cloudflare "
            "tunnel in dev. Recall pushes real-time transcript here, so without it the "
            "bot can speak but cannot hear, and the wake word never fires."
        ),
        examples=["https://thoroughly-liberal-mouse.ngrok-free.app"],
    )
    recall_webhook_token: str = Field(
        default="kindred-dev",
        description=(
            "Shared secret appended to the webhook URL as `?token=`. Recall documents "
            "this as the simpler of its two verification options; the other is an HMAC "
            "signature over the workspace secret. Anything reachable from the internet "
            "needs one of them — the endpoint takes unauthenticated POSTs otherwise."
        ),
    )
    transcript_silence_ms: int = Field(
        default=1000,
        description=(
            "Silence after the last word before an utterance counts as finished. Recall "
            "streams transcript word-by-word, so this is what turns a word stream back "
            "into 'someone finished a sentence'. Lower is snappier and more likely to "
            "cut a speaker off mid-thought."
        ),
    )
    transcript_wake_silence_ms: int = Field(
        default=400,
        description=(
            "The same gap, but once the wake word has been heard. Much shorter, because "
            "somebody is now waiting on an answer out loud and every extra hundred "
            "milliseconds reads as the agent not having heard them."
        ),
    )
    transcript_max_utterance_ms: int = Field(
        default=9000,
        description=(
            "Hard ceiling on how long words may accumulate before the utterance is "
            "flushed anyway. Without this an open microphone never produces a silence "
            "gap, the buffer never closes, and Meet AGI never responds to anything — the "
            "'it only works if you mute at the end' failure."
        ),
    )
    transcript_wake_max_ms: int = Field(
        default=4500,
        description=(
            "The ceiling once a wake word is in the buffer, measured from the wake. "
            "Caps how long a question can take to reach the reasoning pipeline.\n\n"
            "The trade-off runs both ways and only bites when the speaker never pauses: "
            "too low truncates a long question mid-sentence, too high leaves the room "
            "waiting. The default fits any question said at a normal pace. It costs "
            "nothing in the ordinary case, where the silence gap fires first."
        ),
    )

    @property
    def webhook_url(self) -> str | None:
        """Where Recall should POST real-time events, or None if no tunnel is set.

        The trailing slash before the query string is required: Recall calls the URL
        exactly as given, and FastAPI answers the un-slashed path with a 307 that the
        webhook sender does not follow.
        """
        if not self.public_base_url:
            return None
        base = self.public_base_url.rstrip("/")
        return f"{base}/api/recall/webhook/?token={self.recall_webhook_token}"

    recall_api_key: str | None = None
    recall_region: str = Field(
        default="us-west-2",
        description=(
            "Recall API keys are region-scoped: a key from another region returns 401 on "
            "every call. If auth fails, check this before checking the key."
        ),
    )
    recall_timeout_seconds: float = 30.0

    bot_name: str = "Meet AGI"
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
            "Extra hold after a clip's own duration before Meet AGI is considered idle. "
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
