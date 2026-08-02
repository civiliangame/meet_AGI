"""Speech output — making Kindred say something out loud.

Every one of these endpoints queues audio through `SpeechOutput`, which plays one clip
at a time per meeting and honours mute at the moment of playback. Nothing here talks to
Recall directly.

`POST /speak/random` exists to prove the audio path end to end without any reasoning in
the way: dispatch a bot, admit it, hit this, hear it. When something has gone wrong on
stage, it is also the fastest way to find out whether the problem is Recall or Kindred.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..errors import bad_request, conflict, not_found
from ..pipeline import handle_stop
from ..providers.voice import VoiceError, get_sample_clips
from ..runtime import Runtime, get_runtime
from ..schemas import AgentState, Meeting
from ..schemas.speech import (
    ClipList,
    SampleClip,
    SpeakRandomRequest,
    SpeakRequest,
    Utterance,
)
from ..store import store

router = APIRouter(tags=["speech"])


def _require_speakable(meeting_id: str, runtime: Runtime) -> Meeting:
    """Fetch a meeting that is able to speak right now.

    A meeting with no speech channel is one that never had a bot attached — a harness
    replay, or a meeting that has already ended. That is a 409 rather than a 404: the
    meeting exists, it just has no voice.
    """
    meeting = store.meetings.get(meeting_id)
    if meeting is None:
        raise not_found("Meeting", meeting_id)
    if not runtime.speech.is_attached(meeting_id):
        raise conflict(
            "This meeting has no audio channel. Only a live Recall meeting can speak.",
            {"meeting_id": meeting_id, "source": meeting.source, "state": meeting.state},
        )
    if meeting.agent_state == AgentState.MUTED:
        raise conflict("Kindred is muted. Unmute before speaking.", {"meeting_id": meeting_id})
    return meeting


@router.get(
    "/speech/clips",
    response_model=ClipList,
    summary="List the sample clips Kindred can play",
    description=(
        "The stand-in voice while TTS is not wired up. These ids are what `clip_id` "
        "accepts on `/speak`, and the source of the audio `/speak/random` plays."
    ),
)
def list_clips(runtime: Runtime = Depends(get_runtime)) -> ClipList:
    clips = get_sample_clips()
    items = [
        SampleClip(id=clip_id, text=clip.text, duration_ms=clip.duration_ms)
        for clip_id in clips.clip_ids
        if (clip := clips.clip(clip_id))
    ]
    # `provider` is whoever is actually speaking; the clips remain playable either way.
    return ClipList(provider=runtime.voice.name, items=items)


@router.post(
    "/meetings/{meeting_id}/speak",
    response_model=Utterance,
    status_code=202,
    summary="Say something in the meeting",
    description=(
        "Queues audio and returns immediately with the utterance in `queued`. Playback "
        "is serialized per meeting, so this is safe to call several times in a row — "
        "the clips play in order rather than on top of each other.\n\n"
        "Pass `text` to go through the voice provider, or `clip_id` to play a known "
        "sample clip verbatim. While the voice provider is a placeholder, `text` still "
        "produces a canned clip; the utterance is flagged `placeholder: true` and keeps "
        "the requested text in `requested_text` so the UI can be honest about it."
    ),
)
async def speak(
    meeting_id: str, payload: SpeakRequest, runtime: Runtime = Depends(get_runtime)
) -> Utterance:
    _require_speakable(meeting_id, runtime)
    if (payload.text is None) == (payload.clip_id is None):
        raise bad_request("Provide exactly one of `text` or `clip_id`.")

    if payload.clip_id is not None:
        # Resolve the clip now rather than in the worker, so an unknown id is a 400 on
        # this call instead of an utterance that quietly fails to play later.
        try:
            get_sample_clips().clip(payload.clip_id)
        except VoiceError as exc:
            raise bad_request(str(exc), {"clip_id": payload.clip_id}) from exc

    return await runtime.speech.say(meeting_id, payload.text, clip_id=payload.clip_id)


@router.post(
    "/meetings/{meeting_id}/speak/random",
    response_model=list[Utterance],
    status_code=202,
    summary="Play random sample clips",
    description=(
        "Smoke test for the audio path: queues `count` random sample clips, never the "
        "same one twice in a row. Use it to confirm the bot can be heard before "
        "trusting anything upstream of it."
    ),
)
async def speak_random(
    meeting_id: str,
    payload: SpeakRandomRequest | None = None,
    runtime: Runtime = Depends(get_runtime),
) -> list[Utterance]:
    _require_speakable(meeting_id, runtime)
    request = payload or SpeakRandomRequest()

    utterances: list[Utterance] = []
    try:
        for _ in range(request.count):
            utterances.append(await runtime.speech.say_random(meeting_id))
    except VoiceError as exc:
        raise bad_request(str(exc)) from exc
    return utterances


@router.get(
    "/meetings/{meeting_id}/utterances",
    response_model=list[Utterance],
    summary="What Kindred has said in this meeting",
    description=(
        "Oldest first. Records are live — an utterance moves through `queued → speaking "
        "→ played` in place, so polling this shows progress."
    ),
)
def list_utterances(meeting_id: str, runtime: Runtime = Depends(get_runtime)) -> list[Utterance]:
    if meeting_id not in store.meetings:
        raise not_found("Meeting", meeting_id)
    return runtime.speech.history(meeting_id)


@router.post(
    "/meetings/{meeting_id}/interrupt",
    response_model=list[Utterance],
    summary="Cut Kindred off mid-sentence",
    description=(
        "The dashboard's equivalent of saying **\"AGI, stop talking\"** out loud. Drops "
        "the speech queue, cancels the reasoning that was about to produce more speech, "
        "and retracts the clip currently playing through Recall's stop-audio endpoint — "
        "so this really does stop it mid-word, not at the end of the sentence.\n\n"
        "Returns the utterances that were dropped, the one that was playing included."
    ),
)
async def interrupt(meeting_id: str, _runtime: Runtime = Depends(get_runtime)) -> list[Utterance]:
    if meeting_id not in store.meetings:
        raise not_found("Meeting", meeting_id)
    return await handle_stop(meeting_id, source="dashboard", force=True)
