"""Chat sinks — where a `chat_alert` actually goes.

The 500-character limit is enforced here rather than trusted to the model. Google Meet
rejects a longer message outright, so a prompt that usually produces ~200 characters but
occasionally produces 520 would drop that interjection entirely, in the meeting, on
stage. `fit_to_limit` makes overflow impossible by construction; the model's brevity is
a quality goal, not a correctness guarantee.

Truncation is on a word boundary with an ellipsis, because a chat alert cut mid-word
reads like a bug and undermines the thing it is trying to say.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..schemas import CHAT_ALERT_MAX_CHARS

if TYPE_CHECKING:
    from ..integrations.recall.client import RecallClient

logger = logging.getLogger(__name__)


CHAT_PREFIX = "Because you mentioned {topic}:"
"""Every chat message names what prompted it before saying anything else.

A line of reasoning arriving in chat with no anchor reads as a non-sequitur — people
have moved on by the time it lands, and the first thing they do is scroll up to work out
what it is about. Naming the trigger costs ~30 characters of the 500 and removes that
step entirely.

Applied server-side rather than asked for in the prompt: the model is told not to write
it, so there is exactly one place it can come from and it cannot end up doubled or
missing depending on how the model felt about the instruction that turn.
"""

_GENERIC_TOPIC = "that"


def with_trigger_prefix(message: str, topic: str | None) -> str:
    """Prepend "Because you mentioned <topic>:" to a chat message.

    Idempotent — a message that already carries the prefix is returned untouched, so
    re-posting an approved interjection cannot stack it twice.
    """
    message = message.strip()
    if message.lower().startswith("because you mentioned"):
        return message

    cleaned = " ".join((topic or "").split()).strip(" .,:;!?").casefold()
    prefix = CHAT_PREFIX.format(topic=cleaned or _GENERIC_TOPIC)

    # Lowercase a leading capital so the sentence reads as one: "Because you mentioned
    # churn: enterprise churn is 1.2%" rather than "...: Enterprise churn is 1.2%".
    if message and message[0].isupper() and not message[:4].isupper():
        message = message[0].lower() + message[1:]
    return f"{prefix} {message}"


def fit_to_limit(text: str, limit: int = CHAT_ALERT_MAX_CHARS) -> str:
    """Collapse whitespace and hard-cap to `limit` characters.

    Whitespace is normalized first: the model sometimes emits newlines that render as
    blank lines in Meet chat and eat the reader's attention for nothing.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed

    logger.warning("chat alert was %d chars; truncating to %d", len(collapsed), limit)
    clipped = collapsed[: limit - 1]
    # Only break on a word boundary if one is reasonably near the end — otherwise a
    # single long token would cut the message in half.
    space = clipped.rfind(" ")
    if space > limit * 0.6:
        clipped = clipped[:space]
    return clipped.rstrip(" ,;:.—-") + "…"


@runtime_checkable
class ChatSink(Protocol):
    """Somewhere a chat message can be posted."""

    name: str

    async def post(self, message: str, *, pin: bool = False) -> None:
        """Send a message. Implementations enforce the platform's length limit."""
        ...


class RecallChatSink:
    """Posts into a live meeting's chat through a Recall bot."""

    name = "recall"

    def __init__(self, client: RecallClient, bot_id: str) -> None:
        self._client = client
        self._bot_id = bot_id

    @property
    def bot_id(self) -> str:
        return self._bot_id

    async def post(self, message: str, *, pin: bool = False) -> None:
        text = fit_to_limit(message)
        await self._client.send_chat_message(self._bot_id, text, pin=pin)
        logger.info("bot %s posted %d chars to chat", self._bot_id, len(text))


class NullChatSink:
    """Accepts messages and logs them. Backs the fixture harness and dry runs.

    The frontend still sees the interjection over the WebSocket — only the delivery to a
    real meeting is skipped, so the whole ambient loop is demoable with no bot.
    """

    name = "null"

    def __init__(self, label: str = "dry-run") -> None:
        self._label = label
        self.sent: list[str] = []

    async def post(self, message: str, *, pin: bool = False) -> None:
        text = fit_to_limit(message)
        self.sent.append(text)
        logger.info("[%s] would post to chat (%d chars): %s", self._label, len(text), text)
