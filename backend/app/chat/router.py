"""Picks the chat sink for a meeting.

Self-wiring on purpose: a meeting with a `bot_id` gets a `RecallChatSink`, anything else
gets a `NullChatSink`. Nothing has to remember to register a harness meeting, so the
ambient loop posts "to chat" in a fixture replay exactly as it does in a real call — the
only difference is where the bytes land.
"""

from __future__ import annotations

import logging

from .sinks import ChatSink, NullChatSink, RecallChatSink

logger = logging.getLogger(__name__)


class ChatRouter:
    """Resolves and caches one chat sink per meeting."""

    def __init__(self) -> None:
        self._sinks: dict[str, ChatSink] = {}

    def attach(self, meeting_id: str, sink: ChatSink) -> None:
        """Override the sink for a meeting. Tests and dry runs use this."""
        self._sinks[meeting_id] = sink

    def detach(self, meeting_id: str) -> None:
        self._sinks.pop(meeting_id, None)

    def sink_for(self, meeting_id: str) -> ChatSink:
        cached = self._sinks.get(meeting_id)
        if cached is not None:
            return cached

        sink = self._build(meeting_id)
        self._sinks[meeting_id] = sink
        return sink

    def _build(self, meeting_id: str) -> ChatSink:
        from ..store import store

        meeting = store.meetings.get(meeting_id)
        if meeting is None or not meeting.bot_id:
            return NullChatSink(label=meeting_id)

        # Imported here so `app.chat` does not drag the runtime graph in at import time.
        from ..runtime import get_runtime

        runtime = get_runtime()
        if not runtime.recall.configured:
            return NullChatSink(label=meeting_id)
        return RecallChatSink(runtime.recall, meeting.bot_id)

    async def post(self, meeting_id: str, message: str, *, pin: bool = False) -> bool:
        """Post to a meeting's chat. Returns whether it reached a real meeting.

        Never raises: a chat post that fails must not take down the interjection that
        produced it, because the reasoning is still worth showing in the dashboard.
        """
        sink = self.sink_for(meeting_id)
        try:
            await sink.post(message, pin=pin)
        except Exception:
            logger.exception("posting to chat failed for %s", meeting_id)
            return False
        return sink.name != "null"


chat_router = ChatRouter()
