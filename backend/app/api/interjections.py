"""Interjection review — the human-in-the-loop gate.

Used when `settings.autonomy` is `propose`: interjections arrive as `proposed` and wait
for a human to approve or dismiss. `approve` accepts an edited `chat_alert` so the
operator can tighten the wording before it reaches the meeting.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..bus import bus
from ..errors import bad_request, not_found
from ..schemas import (
    AgentState,
    Interjection,
    InterjectionApprove,
    InterjectionDismiss,
    InterjectionStatus,
    utcnow,
)
from ..store import store

router = APIRouter(prefix="/interjections", tags=["interjections"])


def _locate(interjection_id: str) -> tuple[str, int, Interjection]:
    found = store.find_interjection(interjection_id)
    if found is None:
        raise not_found("Interjection", interjection_id)
    meeting_id, index = found
    return meeting_id, index, store.interjections_for(meeting_id)[index]


def _replace(meeting_id: str, index: int, updated: Interjection) -> Interjection:
    store.interjections_for(meeting_id)[index] = updated
    bus.publish_meeting(meeting_id, "interjection.updated", updated)
    return updated


@router.post(
    "/{interjection_id}/approve",
    response_model=Interjection,
    summary="Approve a proposed interjection",
    description="Posts it to the meeting. Optionally overrides the generated chat alert.",
)
def approve(interjection_id: str, payload: InterjectionApprove) -> Interjection:
    meeting_id, index, interjection = _locate(interjection_id)
    if interjection.status not in (
        InterjectionStatus.PROPOSED,
        InterjectionStatus.PROPOSED.value,
    ):
        raise bad_request(
            f"Cannot approve an interjection in state {interjection.status}",
            {"status": interjection.status},
        )
    updates: dict[str, object] = {
        "status": InterjectionStatus.POSTED,
        "posted_at": utcnow(),
    }
    if payload.edited_chat_alert is not None:
        updates["chat_alert"] = payload.edited_chat_alert
    return _replace(meeting_id, index, interjection.model_copy(update=updates))


@router.post(
    "/{interjection_id}/dismiss",
    response_model=Interjection,
    summary="Dismiss a proposed interjection",
    description="Discards it. Never reaches the meeting.",
)
def dismiss(interjection_id: str, payload: InterjectionDismiss) -> Interjection:
    meeting_id, index, interjection = _locate(interjection_id)
    if interjection.status in (InterjectionStatus.POSTED, InterjectionStatus.POSTED.value):
        raise bad_request(
            "Cannot dismiss an interjection that was already posted to the meeting.",
            {"status": interjection.status},
        )
    return _replace(
        meeting_id,
        index,
        interjection.model_copy(
            update={"status": InterjectionStatus.DISMISSED, "error": payload.reason}
        ),
    )


@router.post(
    "/{interjection_id}/speak",
    response_model=Interjection,
    summary="Have Kindred say an interjection out loud",
    description=(
        "Escalates a chat-only interjection to voice. **Milestone 7** performs the "
        "actual TTS; this marks `spoken` and drives the agent state so the UI can be built."
    ),
)
def speak(interjection_id: str) -> Interjection:
    meeting_id, index, interjection = _locate(interjection_id)
    meeting = store.meetings.get(meeting_id)
    if meeting is not None and meeting.agent_state in (AgentState.MUTED, AgentState.MUTED.value):
        raise bad_request("Kindred is muted and cannot speak.", {"meeting_id": meeting_id})
    return _replace(
        meeting_id,
        index,
        interjection.model_copy(
            update={
                "spoken": True,
                "status": InterjectionStatus.POSTED,
                "posted_at": interjection.posted_at or utcnow(),
            }
        ),
    )
