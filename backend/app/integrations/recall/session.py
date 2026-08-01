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
from dataclasses import dataclass

from ...audio import RecallAudioSink
from ...bus import EventBus
from ...bus import bus as default_bus
from ...ids import PREFIX_MEETING, new_id
from ...schemas import (
    AgentState,
    Meeting,
    MeetingSource,
    MeetingState,
    MeetingStateChangedData,
    MeetingPlatform,
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
        """
        session = self._sessions.get(meeting_id)
        if session is None:
            raise LookupError(f"no Recall session for meeting {meeting_id}")
        await session.sink.wait_ready(timeout=timeout)

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
                if await self._apply_status(session, status):
                    return

            await asyncio.sleep(POLL_SECONDS)

    async def _apply_status(self, session: RecallSession, status: str | None) -> bool:
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
            await self._teardown(session.meeting_id, MeetingState.FAILED, error=status)
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

    def _announcement_clip(self, clip_id: str | None):
        voice = self._speech.voice
        loader = getattr(voice, "clip", None)
        silence = getattr(voice, "silence", None)
        if clip_id and loader is not None:
            return loader(clip_id)
        if silence is None:
            raise RecallError(
                f"voice provider {voice.name!r} cannot produce the silent clip Recall "
                f"requires in automatic_audio_output"
            )
        return silence()

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
