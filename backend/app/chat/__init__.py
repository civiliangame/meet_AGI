"""Meeting chat — the channel Kindred types into.

Mirrors `app.audio.sinks`: a protocol, a Recall implementation, and a null one so the
fixture harness exercises the same path with no bot attached.
"""

from .router import ChatRouter, chat_router
from .sinks import ChatSink, NullChatSink, RecallChatSink, fit_to_limit

__all__ = [
    "ChatRouter",
    "ChatSink",
    "NullChatSink",
    "RecallChatSink",
    "chat_router",
    "fit_to_limit",
]
