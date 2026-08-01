"""Speech output — the one path by which Kindred's voice reaches a meeting.

Everything that wants to talk (speech mode, a spoken interjection, the demo CLI) goes
through `SpeechOutput.say()`. Nothing else calls a sink or the Recall client directly.
That is what makes these three properties hold everywhere instead of per caller:

**One clip at a time, in order.** Each meeting gets a queue and a worker. Recall's
`output_audio` returns when the clip is *accepted*, not when it finishes playing, so two
concurrent calls talk over each other in the meeting. The worker holds for the clip's own
duration before starting the next one.

**Mute is honoured at the last possible moment.** The check happens when an utterance is
dequeued, not when it is enqueued, so hitting the kill switch silences speech that was
already queued — which is the entire point of a kill switch.

**`agent_state` reflects reality.** The pill in the UI flips to `speaking` when audio
starts and back to `idle` when it ends, because those transitions live here rather than
in each caller.

Queued speech survives a slow join: the worker awaits the sink's readiness gate, so a
bot still in the Google Meet waiting room holds its utterances instead of losing them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from ..audio import AudioClip, AudioSink
from ..bus import EventBus
from ..bus import bus as default_bus
from ..ids import PREFIX_UTTERANCE, new_id
from ..providers.voice import VoiceError, VoiceProvider
from ..schemas import AgentState, AgentStateChangedData, utcnow
from ..schemas.speech import Utterance, UtteranceStatus
from ..store import Store
from ..store import store as default_store

logger = logging.getLogger(__name__)

DEFAULT_READY_TIMEOUT_SECONDS = 300.0
"""How long an utterance waits for the bot to be admitted before it is dropped.

Generous on purpose: a Google Meet bot sits in the waiting room until a human clicks
Admit, and speech queued during that wait should still be said afterwards.
"""

_HISTORY_LIMIT = 200


@dataclass
class _Channel:
    """Per-meeting speech state: where audio goes, what is waiting, what was said."""

    sink: AudioSink
    queue: asyncio.Queue[Utterance] = field(default_factory=asyncio.Queue)
    history: list[Utterance] = field(default_factory=list)
    worker: asyncio.Task[None] | None = None
    ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS


class SpeechOutput:
    """Serializes and plays Kindred's speech, one queue per meeting."""

    def __init__(
        self,
        *,
        voice: VoiceProvider,
        bus: EventBus | None = None,
        store: Store | None = None,
        tail_padding_ms: int = 400,
    ) -> None:
        self._voice = voice
        self._bus = bus or default_bus
        self._store = store or default_store
        self._tail_padding_ms = tail_padding_ms
        self._channels: dict[str, _Channel] = {}

    @property
    def voice(self) -> VoiceProvider:
        return self._voice

    # --- lifecycle ----------------------------------------------------------------

    def attach(
        self,
        meeting_id: str,
        sink: AudioSink,
        *,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
    ) -> None:
        """Bind a meeting to a sink and start its worker.

        Re-attaching replaces the sink and keeps the queue, so a reconnecting bot picks
        up whatever was waiting to be said.
        """
        channel = self._channels.get(meeting_id)
        if channel is None:
            channel = _Channel(sink=sink, ready_timeout=ready_timeout)
            self._channels[meeting_id] = channel
        else:
            channel.sink = sink
            channel.ready_timeout = ready_timeout

        if channel.worker is None or channel.worker.done():
            channel.worker = asyncio.create_task(
                self._run(meeting_id), name=f"speech:{meeting_id}"
            )
        logger.info("speech output attached to %s via %s sink", meeting_id, sink.name)

    async def detach(self, meeting_id: str) -> None:
        """Stop the worker and discard anything still queued.

        Called when the meeting ends. Pending speech is dropped rather than played,
        because a clip that arrives after the meeting is over helps nobody.
        """
        channel = self._channels.pop(meeting_id, None)
        if channel is None:
            return
        if channel.worker is not None:
            channel.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await channel.worker
        dropped = self._drain(channel, reason="meeting ended")
        if dropped:
            logger.info("dropped %d queued utterance(s) for %s", dropped, meeting_id)

    def is_attached(self, meeting_id: str) -> bool:
        return meeting_id in self._channels

    async def aclose(self) -> None:
        for meeting_id in list(self._channels):
            await self.detach(meeting_id)

    # --- speaking -----------------------------------------------------------------

    async def say(
        self,
        meeting_id: str,
        text: str | None = None,
        *,
        clip_id: str | None = None,
    ) -> Utterance:
        """Queue something for Kindred to say. Returns immediately.

        Give `text` to go through the voice provider, or `clip_id` to play a known
        sample clip verbatim. The returned `Utterance` is the live record — it is
        mutated in place as it moves through `queued → speaking → played`, so a caller
        holding it sees the outcome without polling anything.
        """
        if (text is None) == (clip_id is None):
            raise ValueError("say() takes exactly one of `text` or `clip_id`")

        channel = self._channels.get(meeting_id)
        if channel is None:
            raise LookupError(
                f"no speech channel for meeting {meeting_id}. Is the bot attached "
                f"(is this meeting live)?"
            )

        utterance = Utterance(
            id=new_id(PREFIX_UTTERANCE),
            meeting_id=meeting_id,
            text=text or "",
            clip_id=clip_id,
            status=UtteranceStatus.QUEUED,
            created_at=utcnow(),
        )
        self._remember(channel, utterance)
        await channel.queue.put(utterance)
        return utterance

    async def say_random(self, meeting_id: str) -> Utterance:
        """Queue a random sample clip. The smoke test for the whole output path."""
        clip = self._random_clip()
        return await self.say(meeting_id, clip_id=clip.clip_id)

    def clear(self, meeting_id: str) -> int:
        """Drop everything queued but not yet playing. Returns how many were dropped.

        Barge-in, as far as this endpoint allows: audio already handed to Recall cannot
        be recalled, so the clip currently playing finishes. Streaming Output Media is
        what makes true mid-sentence interruption possible.
        """
        channel = self._channels.get(meeting_id)
        if channel is None:
            return 0
        return self._drain(channel, reason="interrupted")

    async def wait_until_idle(self, meeting_id: str, timeout: float | None = None) -> None:
        """Block until the queue is empty and the last clip has finished playing."""
        channel = self._channels.get(meeting_id)
        if channel is None:
            return
        await asyncio.wait_for(channel.queue.join(), timeout=timeout)

    def queue_depth(self, meeting_id: str) -> int:
        channel = self._channels.get(meeting_id)
        return channel.queue.qsize() if channel else 0

    def history(self, meeting_id: str) -> list[Utterance]:
        """Everything Kindred has said in this meeting, oldest first."""
        channel = self._channels.get(meeting_id)
        return list(channel.history) if channel else []

    # --- worker -------------------------------------------------------------------

    async def _run(self, meeting_id: str) -> None:
        channel = self._channels[meeting_id]
        while True:
            utterance = await channel.queue.get()
            try:
                await self._play(meeting_id, channel, utterance)
            except asyncio.CancelledError:
                self._fail(utterance, "cancelled", status=UtteranceStatus.DROPPED)
                raise
            except Exception as exc:  # one bad utterance must not kill the worker
                logger.exception("utterance %s failed", utterance.id)
                self._fail(utterance, str(exc))
                self._set_agent_state(meeting_id, AgentState.IDLE)
            finally:
                channel.queue.task_done()

    async def _play(self, meeting_id: str, channel: _Channel, utterance: Utterance) -> None:
        if self._is_muted(meeting_id):
            # Checked here, not at enqueue time, so the kill switch also silences
            # everything that was already waiting.
            self._fail(utterance, "muted", status=UtteranceStatus.DROPPED)
            logger.info("utterance %s dropped: Kindred is muted", utterance.id)
            return

        try:
            clip = await self._resolve(utterance)
        except VoiceError as exc:
            self._fail(utterance, f"synthesis failed: {exc}")
            return

        utterance.text = clip.text
        utterance.clip_id = clip.clip_id
        utterance.duration_ms = clip.duration_ms
        utterance.placeholder = clip.is_placeholder
        if clip.is_placeholder:
            utterance.requested_text = clip.placeholder_for

        try:
            await channel.sink.wait_ready(timeout=channel.ready_timeout)
        except (TimeoutError, asyncio.TimeoutError):
            self._fail(
                utterance,
                f"the meeting never became available within {channel.ready_timeout:.0f}s",
                status=UtteranceStatus.DROPPED,
            )
            return

        utterance.status = UtteranceStatus.SPEAKING
        utterance.played_at = utcnow()
        self._set_agent_state(meeting_id, AgentState.SPEAKING, detail=clip.text)

        await channel.sink.play(clip)

        # Recall accepted the clip; it is playing now. Hold for its length plus a little,
        # because playback finishes slightly after the POST returns and clips that start
        # back-to-back clip each other's tails.
        await asyncio.sleep((clip.duration_ms + self._tail_padding_ms) / 1000)

        utterance.status = UtteranceStatus.PLAYED
        self._set_agent_state(meeting_id, AgentState.IDLE)

    async def _resolve(self, utterance: Utterance) -> AudioClip:
        """Turn an utterance into playable audio."""
        if utterance.clip_id:
            clip_loader = getattr(self._voice, "clip", None)
            if clip_loader is None:
                raise VoiceError(
                    f"voice provider {self._voice.name!r} has no fixed clips; "
                    f"cannot play clip_id={utterance.clip_id!r}"
                )
            return clip_loader(utterance.clip_id)

        settings = self._store.settings.voice
        return await self._voice.synthesize(
            utterance.text,
            voice_id=settings.voice_id,
            speaking_rate=settings.speaking_rate,
        )

    def _random_clip(self) -> AudioClip:
        picker = getattr(self._voice, "random_clip", None)
        if picker is None:
            raise VoiceError(f"voice provider {self._voice.name!r} has no sample clips")
        return picker()

    # --- state and bookkeeping ----------------------------------------------------

    def _is_muted(self, meeting_id: str) -> bool:
        meeting = self._store.meetings.get(meeting_id)
        return meeting is not None and meeting.agent_state == AgentState.MUTED

    def _set_agent_state(
        self, meeting_id: str, state: AgentState, *, detail: str | None = None
    ) -> None:
        """Update `agent_state` and tell the frontend.

        Muted wins: an unmute is an explicit action, so nothing here silently clears it.
        """
        meeting = self._store.meetings.get(meeting_id)
        if meeting is not None:
            if meeting.agent_state == AgentState.MUTED:
                return
            meeting.agent_state = state
        self._bus.publish_meeting(
            meeting_id,
            "agent.state_changed",
            AgentStateChangedData(agent_state=state, detail=detail),
        )

    def _fail(
        self,
        utterance: Utterance,
        error: str,
        *,
        status: UtteranceStatus = UtteranceStatus.FAILED,
    ) -> None:
        utterance.status = status
        utterance.error = error

    def _drain(self, channel: _Channel, *, reason: str) -> int:
        dropped = 0
        while True:
            try:
                utterance = channel.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._fail(utterance, reason, status=UtteranceStatus.DROPPED)
            channel.queue.task_done()
            dropped += 1
        return dropped

    @staticmethod
    def _remember(channel: _Channel, utterance: Utterance) -> None:
        channel.history.append(utterance)
        if len(channel.history) > _HISTORY_LIMIT:
            del channel.history[: len(channel.history) - _HISTORY_LIMIT]
