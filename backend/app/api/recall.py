"""Recall real-time webhook — where Kindred's ears are.

Recall POSTs one event per transcript utterance and per participant change to this
endpoint during a live call. It is the only inbound path from the meeting, and the thing
that makes the spoken wake word work at all.

Two rules Recall's docs are explicit about, both load-bearing:

1. **Return 2xx immediately and do the work asynchronously.** Real-time events are
   delivered serially per bot, so a slow handler does not just delay itself — it stalls
   every subsequent utterance in the meeting. Reasoning takes seconds, which is exactly
   the kind of work that must not happen inline.
2. **Verify before processing.** The endpoint is on a public tunnel. This uses the
   documented shared-token option; the alternative is an HMAC signature over the
   workspace secret, which is stronger and worth doing before this is ever more than a
   dev tunnel.

Failures return 2xx anyway. Recall retries a non-2xx up to 60 times at one-second
intervals and then disables the endpoint for the rest of the meeting — so a bug in one
utterance would take the whole meeting's transcript down with it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging

from fastapi import APIRouter, Query, Request, Response

from ..config import get_config
from ..ingest.recall_live import ingest
from ..schemas import Ack

log = logging.getLogger(__name__)

router = APIRouter(prefix="/recall", tags=["recall"])

_background: set[asyncio.Task] = set()


@router.post(
    "/webhook/",
    response_model=Ack,
    summary="Recall real-time events (transcript and participants)",
    description=(
        "Called by Recall.ai during a live meeting, not by the frontend. Configured per "
        "bot via `recording_config.realtime_endpoints`, so it never appears in the "
        "Recall dashboard's webhook list.\n\n"
        "Always answers 200, including on a rejected token or a malformed body: a "
        "non-2xx is retried 60 times and then the endpoint is disabled for the rest of "
        "the meeting."
    ),
)
async def recall_webhook(
    request: Request, token: str = Query(default="", description="Shared secret.")
) -> Response:
    config = get_config()

    # Constant-time compare: a plain `!=` on a secret is a timing oracle, and this
    # endpoint is public by definition.
    if not hmac.compare_digest(token, config.recall_webhook_token):
        log.warning("rejected a Recall webhook with a bad token")
        return Response(status_code=200, content='{"ok":false}', media_type="application/json")

    try:
        payload = await request.json()
    except Exception:
        log.warning("Recall webhook body was not JSON")
        return Response(status_code=200, content='{"ok":false}', media_type="application/json")

    # Hand off and answer immediately. Events are delivered serially per bot, so holding
    # this open for the reasoning pipeline would stall the rest of the meeting.
    task = asyncio.create_task(_dispatch(payload))
    _background.add(task)
    task.add_done_callback(_background.discard)

    return Response(status_code=200, content='{"ok":true}', media_type="application/json")


async def _dispatch(payload: dict) -> None:
    try:
        await ingest.handle(payload)
    except Exception:
        log.exception("failed handling Recall event %s", payload.get("event"))
