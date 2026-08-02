"""Thin async client for the Recall.ai v1 bot API.

Only the surface Meet AGI needs: dispatch a bot, watch it join, push audio into the
meeting, pull it back out. Everything else stays out until there is a caller for it.

Two things about this API are easy to get wrong and are handled here:

1. **API keys are region-scoped.** A `us-west-2` key returns 401 against `us-east-1`,
   which reads like a bad key. `RecallAuthError` says so explicitly.
2. **`output_audio` requires `automatic_audio_output` at bot-creation time.** Recall
   rejects the call otherwise, and the error does not obviously point at bot creation.
   `create_bot` therefore always sets that field — with a silent clip when the caller has
   no join announcement — so the on-demand path is never accidentally disabled.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Terminal bot statuses — polling past these will never change.
TERMINAL_STATUSES = frozenset({"done", "fatal", "call_ended", "media_expired"})
IN_CALL_STATUSES = frozenset({"in_call_not_recording", "in_call_recording"})


class RecallError(RuntimeError):
    """Any failure talking to Recall."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class RecallAuthError(RecallError):
    """401/403. Usually a wrong region rather than a wrong key."""


class RecallNotConfigured(RecallError):
    """No API key. Raised before any network call so the message is actionable."""


def b64_mp3(mp3: bytes) -> str:
    """Standard-alphabet base64 (RFC 4648 §4), which is what Recall specifies."""
    return base64.b64encode(mp3).decode("ascii")


class RecallClient:
    """One client per process. Holds a pooled `httpx.AsyncClient`."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if not self._api_key:
            raise RecallNotConfigured(
                "RECALL_API_KEY is not set. Add it to .env — Meet AGI cannot dispatch a bot "
                "without it. (The fixture harness runs without one.)"
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={
                    "Authorization": f"Token {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: Any | None = None,
    ) -> Any:
        client = self._ensure_client()
        # A rate-limited request was never executed, so retrying it is always safe.
        # A 5xx or a dropped connection might have been executed, so only GET — the one
        # method with no side effects — retries on those. Replaying `output_audio` would
        # mean the bot says the same thing twice in the meeting.
        retry_server_errors = method == "GET"
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                response = await client.request(method, path, json=json, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                if not retry_server_errors or attempt == self._max_retries - 1:
                    raise RecallError(f"{method} {path} failed: {exc}") from exc
                await asyncio.sleep(0.5 * 2**attempt)
                continue

            if response.status_code in (401, 403):
                raise RecallAuthError(
                    f"Recall rejected the API key ({response.status_code}). Keys are "
                    f"region-scoped — confirm RECALL_REGION matches the key's region "
                    f"(currently targeting {self._base_url}).",
                    status=response.status_code,
                    body=response.text[:500],
                )

            retryable = response.status_code == 429 or (
                retry_server_errors and response.status_code >= 500
            )
            if retryable and attempt < self._max_retries - 1:
                delay = _retry_after(response) or 0.5 * 2**attempt
                logger.warning(
                    "recall %s %s -> %s, retrying in %.1fs",
                    method, path, response.status_code, delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                raise RecallError(
                    f"{method} {path} -> {response.status_code}: {response.text[:500]}",
                    status=response.status_code,
                    body=response.text[:500],
                )

            if not response.content:
                return None
            return response.json()

        raise RecallError(f"{method} {path} exhausted retries") from last_error

    # --- Bot lifecycle ------------------------------------------------------------

    async def create_bot(
        self,
        *,
        meeting_url: str,
        bot_name: str,
        join_announcement_mp3: bytes,
        replay_on_participant_join: bool = False,
        start_recording_on: str = "call_join",
        everyone_left_timeout: int = 300,
        webhook_url: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a bot that is allowed to speak, and to hear.

        `join_announcement_mp3` plays once when recording starts. Pass the silent clip
        when there is nothing to announce — the field is mandatory in practice because
        omitting it disables the on-demand `output_audio` endpoint for the bot's whole life.

        `start_recording_on="call_join"` starts recording the moment the bot is admitted.
        The Recall default (`participant_join`) means a bot alone in an empty room never
        starts recording, and audio output is gated on recording having started.

        `webhook_url` turns on real-time transcription. Both halves are required and
        Recall fails quietly if either is missing: a `transcript.provider` with nothing
        listening produces no events, and a `realtime_endpoint` with no provider has
        nothing to send. Without it the bot can talk but not listen.
        """
        automatic_audio_output: dict[str, Any] = {
            "in_call_recording": {
                "data": {"kind": "mp3", "b64_data": b64_mp3(join_announcement_mp3)}
            }
        }
        if replay_on_participant_join:
            # Re-greet late arrivals, but stop after the first minute so the bot does not
            # interrupt a meeting that has already got going.
            automatic_audio_output["in_call_recording"]["replay_on_participant_join"] = {
                "debounce_mode": "trailing",
                "debounce_interval": 10,
                "disable_after": 60,
            }

        # Recall's default is to leave 2 seconds after the bot is alone in the call. That
        # is right for a passive notetaker and wrong here: during a demo the human is
        # often the one still joining, and a bot that vanishes mid-sentence takes the
        # whole point with it.
        automatic_leave = {"everyone_left_timeout": {"timeout": everyone_left_timeout}}

        recording_config: dict[str, Any] = {"start_recording_on": start_recording_on}

        if webhook_url:
            recording_config["transcript"] = {
                "provider": {
                    "recallai_streaming": {
                        # The accuracy-first default runs an async model underneath and
                        # lands utterances seconds late. A copilot answering out loud
                        # needs the low-latency path far more than it needs the last
                        # few points of word accuracy.
                        "mode": "prioritize_low_latency",
                        "language_code": "en",
                    }
                },
                # Per-speaker audio streams, so "who said this" is the platform's answer
                # rather than a guess from voice similarity. Meet AGI attributes claims to
                # named people, so getting the speaker wrong is worse than not flagging.
                "diarization": {"use_separate_streams_when_available": True},
            }
            recording_config["realtime_endpoints"] = [
                {
                    "type": "webhook",
                    "url": webhook_url,
                    "events": [
                        "transcript.data",
                        # Partials never wake Meet AGI — they revise as they arrive and a
                        # half-heard "hey a g..." is exactly the false wake DESIGN.md §12
                        # warns about. They are subscribed for the kill phrase, which is
                        # the one signal worth acting on before the sentence is finished,
                        # and for the live transcript line in the dashboard.
                        "transcript.partial_data",
                        "participant_events.join",
                        "participant_events.leave",
                        "participant_events.speech_on",
                        "participant_events.speech_off",
                    ],
                }
            ]

        payload: dict[str, Any] = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "automatic_leave": automatic_leave,
            "recording_config": recording_config,
            "automatic_audio_output": automatic_audio_output,
        }
        if metadata:
            payload["metadata"] = metadata

        bot = await self._request("POST", "/bot/", json=payload)
        logger.info("recall bot %s dispatched to %s", bot.get("id"), meeting_url)
        return bot

    async def list_bots(self, *, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        """Bots in the workspace, optionally filtered by status.

        Used to find bots a previous backend process left behind. A bot is a
        Recall-side entity: killing the process that dispatched it does not remove it
        from the meeting, it just orphans it until `everyone_left_timeout` expires.
        """
        params = [("status", status) for status in (statuses or [])]
        body = await self._request("GET", "/bot/", params=params or None)
        return list((body or {}).get("results") or [])

    async def get_bot(self, bot_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/bot/{bot_id}/")

    async def leave_call(self, bot_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/bot/{bot_id}/leave_call/", json={})

    # --- Speaking -----------------------------------------------------------------

    async def output_audio(self, bot_id: str, mp3: bytes) -> None:
        """Play an mp3 into the meeting.

        Returns as soon as Recall accepts the clip — **not** when playback finishes.
        Callers that need to avoid overlapping speech must hold for the clip's duration
        themselves; `app.speech.output.SpeechOutput` is the place that does it.
        """
        await self._request(
            "POST",
            f"/bot/{bot_id}/output_audio/",
            json={"kind": "mp3", "b64_data": b64_mp3(mp3)},
        )

    async def stop_output_audio(self, bot_id: str) -> None:
        """Cut off whatever the bot is currently playing.

        `DELETE /bot/{id}/output_audio/`. This is what makes the "AGI stop talking" kill
        phrase a real interruption rather than a promise to be quiet after the current
        sentence — audio already handed to Recall is dropped mid-clip.

        Same `automatic_audio_output` prerequisite as `output_audio`, which `create_bot`
        always satisfies.
        """
        await self._request("DELETE", f"/bot/{bot_id}/output_audio/")

    async def output_video(self, bot_id: str, jpeg: bytes) -> None:
        """Replace the image on the bot's camera tile.

        Meeting platforms model this as the bot's video stream, so this is what
        "showing something in the meeting" means for a participant that is software.
        Recall wants 1280x720 JPEG, at most 1.3 MB.
        """
        await self._request(
            "POST",
            f"/bot/{bot_id}/output_video/",
            json={"kind": "jpeg", "b64_data": base64.b64encode(jpeg).decode("ascii")},
        )

    # --- Chat -----------------------------------------------------------------------

    async def send_chat_message(
        self, bot_id: str, message: str, *, pin: bool = False, to: str = "everyone"
    ) -> None:
        """Post a message to the meeting chat.

        Google Meet caps chat messages at 500 characters and rejects anything longer, so
        callers must have truncated already — `app.chat.sinks` is where that happens.

        `to` is `everyone` on every platform except Zoom. `pin` is used once, for the
        recording disclosure on join.
        """
        await self._request(
            "POST",
            f"/bot/{bot_id}/send_chat_message/",
            json={"to": to, "message": message, "pin": pin},
        )

    # --- Helpers ------------------------------------------------------------------

    @staticmethod
    def latest_status(bot: dict[str, Any]) -> str | None:
        """Most recent status code from a bot payload, e.g. `in_call_recording`."""
        changes = bot.get("status_changes") or []
        if not changes:
            return None
        return changes[-1].get("code")

    async def wait_until_in_call(
        self,
        bot_id: str,
        *,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 3.0,
        require_recording: bool = True,
    ) -> str:
        """Poll until the bot is in the call, then return its status.

        Audio output only reaches the meeting once the bot has been admitted, so this is
        the gate between "bot dispatched" and "bot may speak". Google Meet bots usually
        sit in `in_waiting_room` until a human clicks Admit, which is why the default
        timeout is generous.
        """
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        wanted = {"in_call_recording"} if require_recording else IN_CALL_STATUSES
        last_seen: str | None = None

        while True:
            bot = await self.get_bot(bot_id)
            status = self.latest_status(bot)
            if status != last_seen:
                logger.info("recall bot %s status: %s", bot_id, status)
                last_seen = status
            if status in wanted:
                return status
            if status in TERMINAL_STATUSES:
                raise RecallError(f"bot {bot_id} reached terminal status {status!r} before joining")
            if asyncio.get_running_loop().time() >= deadline:
                raise RecallError(
                    f"bot {bot_id} did not join within {timeout_seconds:.0f}s "
                    f"(last status {status!r}). Did anyone admit it to the meeting?"
                )
            await asyncio.sleep(poll_seconds)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None
