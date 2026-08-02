"""Send Meet AGI into a real meeting and have it speak.

The end-to-end proof of the audio-output path: dispatch a Recall bot, wait to be
admitted, play random sample clips into the call, leave. No server, no frontend, no
reasoning — if this works, everything above it is talking to a voice that works.

    python scripts/demo_speak.py https://meet.google.com/abc-defg-hij
    python scripts/demo_speak.py <url> --clips 5 --gap 3 --announce greeting
    python scripts/demo_speak.py --dry-run          # no bot, no API key, full pipeline

The bot sits in the Google Meet waiting room until a human admits it. That is expected —
admit it and the script carries on. Clips queued during the wait are held, not lost.

Requires RECALL_API_KEY in `.env` (repo root) unless `--dry-run`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.ids import PREFIX_MEETING, new_id  # noqa: E402
from app.integrations.recall import RecallError  # noqa: E402
from app.providers.voice import get_sample_clips  # noqa: E402
from app.runtime import build_runtime  # noqa: E402
from app.schemas import (  # noqa: E402
    AgentState,
    Meeting,
    MeetingSource,
    MeetingState,
    utcnow,
)
from app.store import store  # noqa: E402

logger = logging.getLogger("demo")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("meeting_url", nargs="?", help="Google Meet URL to join.")
    parser.add_argument("--clips", type=int, default=3, help="How many clips to play (default 3).")
    parser.add_argument(
        "--gap", type=float, default=2.0, help="Seconds of silence between clips (default 2)."
    )
    parser.add_argument(
        "--announce",
        default=None,
        metavar="CLIP_ID",
        help=(
            "Sample clip to play automatically when recording starts, e.g. `greeting`. "
            "Defaults to silence — the bot says nothing until told to."
        ),
    )
    parser.add_argument(
        "--admit-timeout",
        type=float,
        default=300.0,
        help="Seconds to wait for someone to admit the bot (default 300).",
    )
    parser.add_argument("--stay", action="store_true", help="Leave the bot in the call at the end.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the whole speech path with no bot and no API key.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging.")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    runtime = build_runtime()
    # Sample clips are assets, not a provider feature — they stay playable under real TTS.
    print(
        f"voice provider : {runtime.voice.name} "
        f"({len(get_sample_clips().clip_ids)} sample clips available)"
    )

    if args.dry_run:
        meeting = Meeting(
            id=new_id(PREFIX_MEETING),
            title="Dry run",
            state=MeetingState.IN_CALL,
            agent_state=AgentState.IDLE,
            source=MeetingSource.HARNESS,
            started_at=utcnow(),
        )
        store.meetings[meeting.id] = meeting
        runtime.attach_dry_run(meeting.id)
        print(f"meeting        : {meeting.id} (dry run - nothing is dispatched)")
    else:
        if not args.meeting_url:
            print("error: meeting_url is required unless --dry-run", file=sys.stderr)
            return 2
        if not runtime.recall_configured:
            print("error: RECALL_API_KEY is not set (.env at the repo root)", file=sys.stderr)
            return 2

        print(f"region         : {runtime.recall.base_url}")
        meeting = await runtime.sessions.start(
            meeting_url=args.meeting_url,
            title="Meet AGI audio smoke test",
            announce_clip_id=args.announce,
        )
        print(f"meeting        : {meeting.id}")
        print(f"bot            : {meeting.bot_id}")
        print("\nwaiting to be admitted — accept the bot in the Meet window...")
        await runtime.sessions.wait_until_ready(meeting.id, timeout=args.admit_timeout)
        print("admitted, and recording. speaking now.\n")

    try:
        for index in range(args.clips):
            utterance = await runtime.speech.say_random(meeting.id)
            # say() returns as soon as the clip is queued; the record is updated in
            # place as it plays, so wait for the queue to drain before reading it.
            await runtime.speech.wait_until_idle(meeting.id)
            status = getattr(utterance.status, "value", utterance.status)
            print(
                f"  [{index + 1}/{args.clips}] {status:<8} "
                f"{utterance.duration_ms:>5}ms  {utterance.clip_id}: {utterance.text}"
            )
            if utterance.error:
                print(f"           error: {utterance.error}")
            if args.gap and index < args.clips - 1:
                await asyncio.sleep(args.gap)
    finally:
        if args.dry_run:
            await runtime.speech.detach(meeting.id)
        elif args.stay:
            print(f"\nleaving bot {meeting.bot_id} in the call. Remove it with:")
            print(f"  POST {runtime.recall.base_url}/bot/{meeting.bot_id}/leave_call/")
        else:
            print("\nleaving the meeting...")
            await runtime.sessions.leave(meeting.id)
        await runtime.aclose()

    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO, which drowns out the interesting lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except TimeoutError:
        print("\nthe bot was never admitted to the meeting.", file=sys.stderr)
        return 1
    except RecallError as exc:
        print(f"\nRecall error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
