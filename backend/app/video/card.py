"""Meet AGI's video tile — a status card rendered onto the bot's camera.

Meeting platforms have no notion of "a bot with a UI": whatever you want people to see
is a participant's video stream. Recall exposes two ways to drive it, and this uses the
cheaper one:

- **`POST /bot/{id}/output_video/`** with a base64 JPEG — a still image, replaceable at
  any time. That is what this module pushes, on every state change, which reads as a
  live card even though it is technically a slideshow.
- **Output Media** streams real MP4/GIF over a socket. Necessary for animation or an
  avatar, and a much bigger lift; nothing here forecloses it.

Constraints come from Recall's guidance and are why the layout looks the way it does:
1280x720 JPEG, ≤1.3 MB, text ≥50px, solid colours, and — because Google Meet crops the
tile at small sizes — content kept inside a centre safe zone rather than the full frame.

Pushes are coalesced. Meet AGI can change state three times in a second, and each push is
a ~150 KB upload; sending every one would spend the meeting's bandwidth animating a
label nobody is reading that fast.
"""

from __future__ import annotations

import asyncio
import io
import logging
import textwrap
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1280, 720
SAFE_X, SAFE_Y = 160, 90  # Google Meet crops the edges on small tiles.

MIN_PUSH_INTERVAL = 0.6
"""Seconds between uploads. Coalesces bursts of state changes into one frame."""

_BG = (13, 17, 23)
_FG = (237, 242, 247)
_MUTED = (125, 138, 156)
_RULE = (38, 45, 56)

# One colour per agent state — the pill in the dashboard uses the same vocabulary, and
# the audience learns to read it within a minute of watching.
_STATE_COLOR = {
    "idle": (86, 156, 214),
    "listening": (72, 187, 120),
    "thinking": (214, 158, 46),
    "speaking": (159, 122, 234),
    "muted": (120, 120, 128),
}
_STATE_LABEL = {
    "idle": "LISTENING AMBIENTLY",
    "listening": "HEARD YOU",
    "thinking": "SEARCHING DOCUMENTS",
    "speaking": "SPEAKING",
    "muted": "MUTED",
}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """A real TrueType face, falling back to the bitmap default.

    Pillow's default font ignores `size`, so on a box with no fonts the card would
    render 10px text on a 720p canvas. Worth trying several names.
    """
    candidates = (
        ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"]
        if bold
        else ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"]
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


@dataclass(frozen=True)
class CardState:
    """Everything the card shows. Hashable, so an unchanged card is never re-pushed."""

    agent_state: str = "idle"
    wake_word: str = "Hey AGI"
    headline: str = ""
    citation: str = ""
    detail: str = ""


def render(state: CardState) -> bytes:
    """Draw the card and return JPEG bytes."""
    image = Image.new("RGB", (WIDTH, HEIGHT), _BG)
    draw = ImageDraw.Draw(image)

    accent = _STATE_COLOR.get(state.agent_state, _STATE_COLOR["idle"])
    draw.rectangle([0, 0, WIDTH, 12], fill=accent)

    y = SAFE_Y
    draw.text((SAFE_X, y), "MEET AGI", font=_font(46, bold=True), fill=_FG)
    y += 66

    label = _STATE_LABEL.get(state.agent_state, state.agent_state.upper())
    draw.text((SAFE_X, y), label, font=_font(52, bold=True), fill=accent)
    y += 84

    draw.line([(SAFE_X, y), (WIDTH - SAFE_X, y)], fill=_RULE, width=3)
    y += 40

    if state.headline:
        # 50px minimum per Recall's guidance; wrapped by character count because the
        # safe zone is fixed and the text is short.
        for line in textwrap.wrap(state.headline, width=42)[:4]:
            draw.text((SAFE_X, y), line, font=_font(50, bold=True), fill=_FG)
            y += 62
        if state.citation:
            y += 14
            draw.text((SAFE_X, y), state.citation[:70], font=_font(34), fill=_MUTED)
    else:
        draw.text(
            (SAFE_X, y),
            f'Say "{state.wake_word}" to ask',
            font=_font(50),
            fill=_MUTED,
        )
        y += 70
        if state.detail:
            draw.text((SAFE_X, y), state.detail[:70], font=_font(34), fill=_MUTED)

    buffer = io.BytesIO()
    # Quality 88 sits in Recall's recommended 85-95 band and lands ~150 KB, well under
    # the 1.3 MB ceiling.
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue()


class VideoCard:
    """Keeps one meeting's bot tile in sync with what Meet AGI is doing."""

    def __init__(self) -> None:
        self._last: dict[str, CardState] = {}
        self._headline: dict[str, tuple[str, str]] = {}
        self._pending: dict[str, asyncio.Task] = {}

    async def update(self, meeting_id: str, state: CardState) -> None:
        """Push a new card if it differs from what is already on screen."""
        if self._last.get(meeting_id) == state:
            return
        self._last[meeting_id] = state

        existing = self._pending.get(meeting_id)
        if existing is not None and not existing.done():
            # A newer state supersedes one still waiting out the coalescing window.
            existing.cancel()

        task = asyncio.create_task(self._push_soon(meeting_id), name=f"card:{meeting_id}")
        self._pending[meeting_id] = task

    async def _push_soon(self, meeting_id: str) -> None:
        try:
            await asyncio.sleep(MIN_PUSH_INTERVAL)
        except asyncio.CancelledError:
            return

        state = self._last.get(meeting_id)
        if state is None:
            return

        from ..runtime import get_runtime
        from ..store import store

        meeting = store.meetings.get(meeting_id)
        if meeting is None or not meeting.bot_id:
            return

        try:
            runtime = get_runtime()
            if not runtime.recall.configured:
                return
            jpeg = await asyncio.to_thread(render, state)
            await runtime.recall.output_video(meeting.bot_id, jpeg)
            log.debug("pushed video card to %s (%s)", meeting_id, state.agent_state)
        except Exception:
            # The tile is decoration. Never let it interfere with speaking or reasoning.
            log.warning("could not update the video card for %s", meeting_id, exc_info=True)

    def forget(self, meeting_id: str) -> None:
        self._last.pop(meeting_id, None)
        self._headline.pop(meeting_id, None)
        task = self._pending.pop(meeting_id, None)
        if task is not None and not task.done():
            task.cancel()

    # --- bus observer ------------------------------------------------------------------

    def observe(self, frame: dict) -> None:
        """React to any live event. Registered once, at startup.

        Watching the bus rather than being called from each site is what makes the tile
        correct: `agent.state_changed` is published from the engine, the speech queue,
        *and* the `/ask` endpoint, and a hand-wired call from only one of them leaves the
        card showing `thinking` long after Meet AGI stopped.
        """
        meeting_id = frame.get("meeting_id")
        if not meeting_id:
            return
        event = frame.get("type")
        data = frame.get("data")

        if event == "interjection.proposed":
            citation = (getattr(data, "citations", None) or [None])[0]
            where = ""
            if citation is not None:
                where = citation.filename
                if citation.page:
                    where += f" p.{citation.page}"
            self._headline[meeting_id] = (getattr(data, "headline", "") or "", where)
            return

        if event != "agent.state_changed":
            return

        from ..store import store

        headline, citation = self._headline.get(meeting_id, ("", ""))
        state = getattr(data, "agent_state", None) or "idle"
        self._schedule(
            meeting_id,
            CardState(
                agent_state=str(getattr(state, "value", state)),
                wake_word=store.settings.wake_word,
                headline=headline,
                citation=citation,
                detail=str(getattr(data, "detail", "") or ""),
            ),
        )

    def _schedule(self, meeting_id: str, state: CardState) -> None:
        """`update()` from a synchronous context, when a loop is running."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # sync test context; the tile is decoration
        asyncio.create_task(self.update(meeting_id, state), name=f"card:{meeting_id}")


card = VideoCard()


def attach_to_bus() -> None:
    """Start mirroring live events onto the bot's video tile."""
    from ..bus import bus

    bus.observe(card.observe)
