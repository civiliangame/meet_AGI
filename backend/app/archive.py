"""Session persistence.

The dashboard exists to review meetings *after* they happen, which is impossible if a
backend restart erases them. This module writes each session to disk and restores it on
boot.

One JSON bundle per session under `data/sessions/{meeting_id}.json`, holding the meeting
record, the finalized transcript, and the interjections. Deliberately not a database:

- A session is only ever read as a whole (the review page fetches the bundle) or listed
  by its header, so there is nothing to query and no join to optimize.
- One file per session means a corrupt write costs one meeting, not the archive.
- Zero infrastructure. `pip install -e .` and it works — which is the same reason the
  rest of Milestone 0 has no database.

Postgres becomes the right answer when sessions need cross-meeting search. Swap this
module; nothing outside it knows the storage format.

Writes are debounced. A background task flushes every `FLUSH_INTERVAL_SECONDS`, and
decides what to write from meeting *state* rather than from callers remembering to
announce a change: live sessions are rewritten each pass, finished ones exactly once.
Transcript segments arrive several times a second, so rewriting the bundle on each would
spend the meeting doing file IO.

Deriving the work from state also keeps this module decoupled — no `mark_dirty` call has
to be threaded through the ingestion, reasoning, and speech paths. `mark_dirty` exists
for the cases state cannot reveal, like approving an interjection after the meeting has
already ended.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from pydantic import ValidationError

from .config import REPO_ROOT
from .schemas import Interjection, Meeting, MeetingState, TranscriptSegment
from .store import refresh_meeting_stats, store

log = logging.getLogger(__name__)

SESSIONS_DIR = REPO_ROOT / "data" / "sessions"
FLUSH_INTERVAL_SECONDS = 5.0
SCHEMA_VERSION = 1

_dirty: set[str] = set()
_finalized: set[str] = set()
"""Sessions already written since they ended. Stops rewriting a finished bundle
every flush for the life of the process."""
_flusher: asyncio.Task[None] | None = None


def mark_dirty(meeting_id: str) -> None:
    """Queue a session for the next flush. Cheap; call it freely."""
    _dirty.add(meeting_id)


def _path_for(meeting_id: str) -> Path:
    return SESSIONS_DIR / f"{meeting_id}.json"


def save(meeting_id: str) -> bool:
    """Write one session bundle. Returns False if there is nothing to write."""
    meeting = store.meetings.get(meeting_id)
    if meeting is None:
        return False

    # Recompute here so the counts are correct both on disk and in the live store —
    # which means the session list is accurate without every mutation site having to
    # remember to bump a counter.
    refresh_meeting_stats(meeting)

    bundle = {
        "schema_version": SCHEMA_VERSION,
        "meeting": meeting.model_dump(mode="json"),
        "transcript": [
            segment.model_dump(mode="json")
            for segment in store.segments_for(meeting_id)
            if segment.is_final
        ],
        "interjections": [
            item.model_dump(mode="json") for item in store.interjections_for(meeting_id)
        ],
    }

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    destination = _path_for(meeting_id)
    # Write to a sibling temp file and replace, so an interrupted write cannot leave a
    # half-written bundle where a valid one used to be.
    temp = destination.with_suffix(".json.tmp")
    try:
        temp.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
        temp.replace(destination)
    except OSError:
        log.exception("failed to archive session %s", meeting_id)
        temp.unlink(missing_ok=True)
        return False

    _dirty.discard(meeting_id)
    return True


def save_now(meeting_id: str) -> bool:
    """Flush a session immediately. Use at session end, not per-utterance."""
    return save(meeting_id)


def delete(meeting_id: str) -> bool:
    _dirty.discard(meeting_id)
    path = _path_for(meeting_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def load_all() -> int:
    """Restore archived sessions into the store. Returns how many loaded.

    A session that was live when the process died is marked `ended`: the bot is long
    gone, and leaving it `in_call` would show a meeting the dashboard could never
    receive another event for.
    """
    if not SESSIONS_DIR.exists():
        return 0

    loaded = 0
    for path in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
            meeting = Meeting.model_validate(bundle["meeting"])
            segments = [TranscriptSegment.model_validate(s) for s in bundle.get("transcript", [])]
            interjections = [
                Interjection.model_validate(i) for i in bundle.get("interjections", [])
            ]
        except (OSError, ValueError, KeyError, ValidationError):
            # One unreadable bundle must not stop the rest of the archive loading.
            log.exception("skipping unreadable session archive %s", path.name)
            continue

        if meeting.state in (MeetingState.IN_CALL, MeetingState.JOINING):
            meeting.state = MeetingState.ENDED
            meeting.error = meeting.error or "Backend restarted while this session was live."

        store.meetings[meeting.id] = meeting
        store.segments[meeting.id] = segments
        store.interjections[meeting.id] = interjections
        loaded += 1

    if loaded:
        log.info("restored %d archived session(s)", loaded)
    return loaded


async def _flush_loop() -> None:
    """Keep live sessions archived, and archive each finished one exactly once.

    Deriving what to write from meeting state means no `mark_dirty` call has to be
    threaded through the ingestion, reasoning, and speech paths — which are owned by
    another workstream and would drift. `mark_dirty` stays available for changes that
    state alone cannot reveal, like approving an interjection after the meeting.
    """
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        try:
            for meeting in list(store.meetings.values()):
                is_live = meeting.state in (MeetingState.IN_CALL, MeetingState.JOINING)
                if meeting.id in _dirty or is_live:
                    save(meeting.id)
                    if not is_live:
                        _finalized.add(meeting.id)
                elif meeting.id not in _finalized:
                    save(meeting.id)
                    _finalized.add(meeting.id)
        except Exception:  # noqa: BLE001 — a bad session must not kill the flusher
            log.exception("session autosave pass failed")


def start_autosave() -> None:
    global _flusher
    if _flusher is None or _flusher.done():
        _flusher = asyncio.create_task(_flush_loop(), name="session-autosave")


async def stop_autosave() -> None:
    """Cancel the flusher and write everything still pending."""
    global _flusher
    if _flusher is not None:
        _flusher.cancel()
        try:
            await _flusher
        except asyncio.CancelledError:
            pass
        _flusher = None
    for meeting_id in list(_dirty):
        save(meeting_id)
