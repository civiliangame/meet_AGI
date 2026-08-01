"""Recall sessions — a dispatched bot bound to a `Meeting`.

One session ties together the three things that have to stay in step for Kindred to
speak into a real meeting:

- the **bot** Recall is running,
- the **`Meeting`** record the frontend reads,
- the **speech channel** whose audio has nowhere to go until the bot is admitted.

A watcher task polls the bot's status and moves the other two along with it: the meeting
goes `joining → in_call → ended`, and the sink's readiness gate opens the moment the bot
is actually recording. Speech queued while the bot sat in the waiting room is played
when it gets in, rather than being lost.

Polling rather than webhooks on purpose: webhooks need a public HTTPS endpoint, and
requiring ngrok to be up before the bot can speak is exactly the kind of setup step that
eats a demo. Real-time transcript ingestion needs that tunnel anyway and can move to it
later; audio output does not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from ...audio import AudioClip, RecallAudioSink
from ...bus import EventBus
from ...bus import bus as default_bus
from ...ids import PREFIX_MEETING, new_id
from ...providers.voice import get_sample_clips
from ...schemas import (
    AgentState,
    Meeting,
    MeetingPlatform,
    MeetingSource,
    MeetingState,
    MeetingStateChangedData,
    utcnow,
)
from ...speech.output import SpeechOutput
from ...store import Store
from ...store import store as default_store
from .client import RecallClient, RecallError

logger = logging.getLogger(__name__)

POLL_SECONDS = 3.0

# Bot status → what it means for the meeting record.
_ENDED_STATUSES = frozenset({"call_ended", "done", "media_expired"})
_FAILED_STATUSES = frozenset({"fatal", "recording_permission_denied"})
_READY_STATUS = "in_call_recording"
_IN_CALL_STATUSES = frozenset({"in_call_not_recording", _READY_STATUS})


@dataclass
class RecallSession:
    meeting_id: str
    bot_id: str
    sink: RecallAudioSink
    watcher: asyncio.Task[None] | None = None
    last_status: str | None = None
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when the bot has left, failed, or was never able to join.

    Without this, a caller waiting to be admitted would sit out its whole timeout after
    the bot has already died — a bad meeting link fails in seconds, so it should be
    reported in seconds.
    """


class RecallSessionManager:
    """Dispatches bots and keeps their `Meeting` records honest."""

    def __init__(
        self,
        *,
        client: RecallClient,
        speech: SpeechOutput,
        store: Store | None = None,
        bus: EventBus | None = None,
        bot_name: str = "Kindred",
    ) -> None:
        self._client = client
        self._speech = speech
        self._store = store or default_store
        self._bus = bus or default_bus
        self._bot_name = bot_name
        self._sessions: dict[str, RecallSession] = {}

    def get(self, meeting_id: str) -> RecallSession | None:
        return self._sessions.get(meeting_id)

    @property
    def client(self) -> RecallClient:
        return self._client

    # --- lifecycle ----------------------------------------------------------------

    async def start(
        self,
        *,
        meeting_url: str,
        title: str | None = None,
        announce_clip_id: str | None = None,
        replay_on_participant_join: bool = False,
    ) -> Meeting:
        """Dispatch a bot and register the meeting.

        `announce_clip_id` plays once when recording starts — a spoken disclosure that
        Kindred is present. With no clip id the bot still gets a silent one, because
        Recall disables the on-demand `output_audio` endpoint for any bot created without
        `automatic_audio_output`.
        """
        announcement = self._announcement_clip(announce_clip_id)

        meeting = Meeting(
            id=new_id(PREFIX_MEETING),
            title=title or "Live meeting",
            meeting_url=meeting_url,
            platform=MeetingPlatform.GOOGLE_MEET,
            state=MeetingState.JOINING,
            agent_state=AgentState.IDLE,
            source=MeetingSource.RECALL,
        )

        bot = await self._client.create_bot(
            meeting_url=meeting_url,
            bot_name=self._bot_name,
            join_announcement_mp3=announcement.mp3,
            replay_on_participant_join=replay_on_participant_join,
            metadata={"meeting_id": meeting.id},
        )
        meeting.bot_id = bot["id"]
        self._store.meetings[meeting.id] = meeting
        self._publish_state(meeting)

        sink = RecallAudioSink(self._client, meeting.bot_id)
        self._speech.attach(meeting.id, sink)

        session = RecallSession(meeting_id=meeting.id, bot_id=meeting.bot_id, sink=sink)
        session.watcher = asyncio.create_task(
            self._watch(session), name=f"recall-watch:{meeting.id}"
        )
        self._sessions[meeting.id] = session

        logger.info("meeting %s → bot %s joining %s", meeting.id, meeting.bot_id, meeting_url)
        return meeting

    async def leave(self, meeting_id: str) -> Meeting:
        """Remove the bot from the call and close the meeting out."""
        meeting = self._require_meeting(meeting_id)
        session = self._sessions.get(meeting_id)

        if session is not None:
            with contextlib.suppress(RecallError):
                await self._client.leave_call(session.bot_id)
        await self._teardown(meeting_id, MeetingState.ENDED)
        return meeting

    async def aclose(self) -> None:
        for meeting_id in list(self._sessions):
            await self._teardown(meeting_id, MeetingState.ENDED)

    # --- readiness ----------------------------------------------------------------

    async def wait_until_ready(self, meeting_id: str, timeout: float = 300.0) -> None:
        """Block until the bot is in the call and recording, so speech will be heard.

        Callers do not have to use this — speech queued beforehand is held by the sink
        and played on admission. It exists for scripts that want to report "waiting to be
        admitted" rather than sit silently.

        Raises `RecallError` as soon as the bot dies instead of waiting out `timeout`: a
        bad meeting link fails within seconds, and five minutes of silence is a terrible
        way to learn that.
        """
        session = self._sessions.get(meeting_id)
        if session is None:
            raise LookupError(f"no Recall session for meeting {meeting_id}")

        ready = asyncio.ensure_future(session.sink.wait_ready())
        finished = asyncio.ensure_future(session.finished.wait())
        try:
            done, _ = await asyncio.wait(
                {ready, finished}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (ready, finished):
                if not task.done():
                    task.cancel()

        if ready in done:
            return
        if finished in done:
            meeting = self._store.meetings.get(meeting_id)
            raise RecallError(
                f"bot {session.bot_id} left before it could speak"
                + (f": {meeting.error}" if meeting and meeting.error else "")
            )
        raise TimeoutError(
            f"bot {session.bot_id} was not admitted within {timeout:.0f}s "
            f"(last status {session.last_status!r})"
        )

    # --- watcher ------------------------------------------------------------------

    async def _watch(self, session: RecallSession) -> None:
        """Poll the bot and keep the meeting record and readiness gate in sync."""
        while True:
            try:
                bot = await self._client.get_bot(session.bot_id)
            except RecallError as exc:
                # Transient API trouble should not tear down a live meeting; log and
                # retry on the next tick.
                logger.warning("polling bot %s failed: %s", session.bot_id, exc)
                await asyncio.sleep(POLL_SECONDS)
                continue

            status = self._client.latest_status(bot)
            if status != session.last_status:
                logger.info("meeting %s bot status: %s", session.meeting_id, status)
                session.last_status = status
                if await self._apply_status(session, status, bot):
                    return

            await asyncio.sleep(POLL_SECONDS)

    async def _apply_status(
        self, session: RecallSession, status: str | None, bot: dict[str, object]
    ) -> bool:
        """Reflect a bot status change. Returns True when the session is finished."""
        meeting = self._store.meetings.get(session.meeting_id)
        if meeting is None:
            return True

        if status == _READY_STATUS:
            # Recording has started, which is the point audio output actually reaches
            # the meeting. Open the gate; anything queued during the wait plays now.
            session.sink.mark_ready()
            if meeting.state != MeetingState.IN_CALL:
                meeting.state = MeetingState.IN_CALL
                meeting.started_at = meeting.started_at or utcnow()
                self._publish_state(meeting)
            return False

        if status in _IN_CALL_STATUSES:
            if meeting.state != MeetingState.IN_CALL:
                meeting.state = MeetingState.IN_CALL
                meeting.started_at = meeting.started_at or utcnow()
                self._publish_state(meeting)
            return False

        if status in _FAILED_STATUSES:
            await self._teardown(
                session.meeting_id, MeetingState.FAILED, error=_describe_failure(bot, status)
            )
            return True

        if status in _ENDED_STATUSES:
            await self._teardown(session.meeting_id, MeetingState.ENDED)
            return True

        # joining_call, in_waiting_room, and anything Recall adds later: still pending.
        session.sink.mark_not_ready()
        return False

    # --- helpers ------------------------------------------------------------------

    async def _teardown(
        self, meeting_id: str, state: MeetingState, *, error: str | None = None
    ) -> None:
        session = self._sessions.pop(meeting_id, None)
        if session is not None:
            session.sink.mark_not_ready()
            # Release anyone waiting to be admitted before cancelling the watcher, so a
            # failed join is reported immediately rather than at their timeout.
            session.finished.set()
            if session.watcher is not None and session.watcher is not asyncio.current_task():
                session.watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await session.watcher

        await self._speech.detach(meeting_id)

        meeting = self._store.meetings.get(meeting_id)
        if meeting is None:
            return
        meeting.state = state
        meeting.agent_state = AgentState.IDLE
        meeting.ended_at = meeting.ended_at or utcnow()
        if error:
            meeting.error = error
        if meeting.started_at:
            meeting.stats.duration_seconds = int(
                (meeting.ended_at - meeting.started_at).total_seconds()
            )
        self._publish_state(meeting)
        logger.info("meeting %s ended (%s)", meeting_id, state)

    def _announcement_clip(self, clip_id: str | None) -> AudioClip:
        """The clip that plays automatically when recording starts.

        Always resolved from the sample assets: the silent placeholder is a fixed file,
        not something a TTS provider should be asked to generate.
        """
        clips = get_sample_clips()
        return clips.clip(clip_id) if clip_id else clips.silence()

    def _require_meeting(self, meeting_id: str) -> Meeting:
        meeting = self._store.meetings.get(meeting_id)
        if meeting is None:
            raise LookupError(f"no meeting {meeting_id}")
        return meeting

    def _publish_state(self, meeting: Meeting) -> None:
        self._bus.publish_meeting(
            meeting.id,
            "meeting.state_changed",
            MeetingStateChangedData(
                state=meeting.state, agent_state=meeting.agent_state, error=meeting.error
            ),
        )


def _describe_failure(bot: dict[str, object], status: str | None) -> str:
    """Human-readable reason a bot failed, for `Meeting.error`.

    The status code alone is not actionable — `fatal` covers everything from a typo in
    the meeting link to being ejected by the host. Recall puts the useful part in the
    status change's `sub_code` and `message`, so surface those:

        "fatal (meeting_not_found): No meeting was found at the given link."
    """
    changes = bot.get("status_changes") or []
    last = changes[-1] if isinstance(changes, list) and changes else {}
    if not isinstance(last, dict):
        last = {}

    described = status or "unknown"
    if sub_code := last.get("sub_code"):
        described = f"{described} ({sub_code})"
    if message := last.get("message"):
        described = f"{described}: {message}"
    return described
