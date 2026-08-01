"""The live objects the API and the demo scripts share.

Wiring that is neither config nor a request handler lives here: the Recall client, the
voice provider, the speech queue, and the session manager, built once and handed to
whoever needs them.

`build_runtime()` is what a script calls; `get_runtime()` is what a request handler
calls. Both produce the same object graph, which is why `scripts/demo_speak.py` exercises
the same code path the HTTP API does rather than a parallel one that can drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .audio import NullAudioSink
from .config import AppConfig, get_config
from .integrations.recall import RecallClient, RecallSessionManager
from .providers.voice import VoiceProvider, get_voice_provider
from .speech import SpeechOutput

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    config: AppConfig
    voice: VoiceProvider
    recall: RecallClient
    speech: SpeechOutput
    sessions: RecallSessionManager

    @property
    def recall_configured(self) -> bool:
        return self.recall.configured

    def attach_dry_run(self, meeting_id: str, label: str = "dry-run") -> None:
        """Point a meeting's speech at a sink that goes nowhere.

        Lets the fixture harness and offline tests run the full speech path — queueing,
        state transitions, live events, real clip durations — with no bot and no API key.
        """
        self.speech.attach(meeting_id, NullAudioSink(label))

    async def aclose(self) -> None:
        await self.sessions.aclose()
        await self.speech.aclose()
        await self.recall.aclose()


def build_runtime(config: AppConfig | None = None) -> Runtime:
    config = config or get_config()
    voice = get_voice_provider(config.voice_provider)
    recall = RecallClient(
        api_key=config.recall_api_key,
        base_url=config.recall_base_url,
        timeout_seconds=config.recall_timeout_seconds,
    )
    speech = SpeechOutput(voice=voice, tail_padding_ms=config.speech_tail_padding_ms)
    sessions = RecallSessionManager(
        client=recall, speech=speech, bot_name=config.bot_name
    )
    if not recall.configured:
        logger.warning(
            "RECALL_API_KEY is not set — Kindred can run the harness but cannot join a "
            "real meeting."
        )
    return Runtime(config=config, voice=voice, recall=recall, speech=speech, sessions=sessions)


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    """Process-wide runtime, built on first use. FastAPI dependency."""
    global _runtime
    if _runtime is None:
        _runtime = build_runtime()
    return _runtime


async def shutdown_runtime() -> None:
    global _runtime
    if _runtime is not None:
        await _runtime.aclose()
        _runtime = None
