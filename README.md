# meet_AGI

**A Google Meet copilot that actually speaks.**

The agent is called **Kindred**. It joins your meeting as a participant, hears every
speaker on a separate audio track, knows who each person is, and stays quiet.

- **Ambient mode (default).** Kindred listens for factual claims and checks them against
  your documents. When it finds a real conflict, it flags it in the meeting chat and puts
  the full reasoning — with citations — in the dashboard.
- **Speech mode.** Say *"Kindred"* and it wakes up, listens to your question, asks a
  clarifying question if it needs one, and answers out loud in the meeting.

Built at the AGI House hackathon on [Inworld](https://inworld.ai) (voice),
[Character.AI](https://character.ai) (persona), and [Tenstorrent](https://tenstorrent.com)
(ambient triage inference), with [Recall.ai](https://recall.ai) for meeting I/O.

---

## Status

The API contract is live and the backend runs. Audio output into a real meeting works.
Reasoning is still canned. See **[DESIGN.md](./DESIGN.md)** for the full architecture.

| Milestone | State |
|---|---|
| 0 — Schemas, OpenAPI, generated TS, fixture harness | ✅ done |
| 1 — Config CRUD (people, documents, integrations, settings) | ✅ done, in-memory |
| 2 — Harness emits the full live event stream | ✅ done |
| 3 — Document ingestion → pgvector retrieval | ⬜ simulated on a timer |
| 4 — Ambient loop: triage → reason → interjection | ⬜ canned content |
| 5 — Recall bot join + per-speaker transcript | 🟡 bot join + audio out done; transcript ingestion open |
| 7 — Speech mode with real TTS | 🟡 path works on sample clips |

## Quick links

- [**API contract**](./DESIGN.md#8-api-contract) ← the frontend/backend boundary
- [**frontend/README.md**](./frontend/README.md) ← start here if you're building the UI
- [Architecture](./DESIGN.md#3-architecture) · [Build order](./DESIGN.md#11-build-order)
- [Verified platform capabilities](./DESIGN.md#2-confirmed-platform-capabilities)

## Run it

No database, no API keys, no internet connection required.

```bash
cd backend
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e .     # Windows
# .venv/bin/pip install -e .                       # macOS / Linux
./.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

Swagger UI at **http://localhost:8000/docs**. Then start a fake meeting that emits the
real event stream:

```bash
curl -X POST localhost:8000/api/dev/harness/start \
  -H 'content-type: application/json' \
  -d '{"fixture_id":"q3_revenue_review","speed":6,"loop":true}'
```

Copy the returned `id` and connect to `ws://localhost:8000/api/meetings/{id}/live`.

Tests:

```bash
cd backend && ./.venv/Scripts/python.exe -m pytest tests/ -q
```

## The contract

`backend/app/schemas/` (Pydantic) is the single source of truth. FastAPI derives OpenAPI
from it; `./scripts/gen-types.sh` derives `frontend/src/lib/api/generated.ts` from that.
**Never hand-write an API type in the frontend.**

Run `./scripts/gen-types.sh` after any schema change and commit both `openapi.json` and
the generated TypeScript, so the frontend always has current types even when the backend
is not running.

Adding fields, enum variants, and event types is free and unannounced. **Renames and
removals get a heads-up first** — two people are building against this in parallel.

## Making Kindred speak

Audio output runs end to end today, on sample clips rather than real TTS. Send a bot into
a meeting and have it talk:

```bash
python scripts/demo_speak.py https://meet.google.com/abc-defg-hij --clips 3
python scripts/demo_speak.py --dry-run          # whole path, no bot, no API key
```

The bot waits in the Google Meet lobby until you admit it, then plays random clips.
Clips queued during that wait are held, not dropped.

Over HTTP:

```bash
curl -X POST localhost:8000/api/meetings -H 'content-type: application/json' \
  -d '{"meeting_url":"https://meet.google.com/abc-defg-hij"}'
curl -X POST localhost:8000/api/meetings/$MTG/speak -H 'content-type: application/json' \
  -d '{"clip_id":"flag_revenue"}'
curl -X POST localhost:8000/api/meetings/$MTG/speak/random -d '{"count":3}'
```

`GET /api/speech/clips` lists the clips. `/speak` also works on a harness meeting — the
audio goes nowhere, but the queueing, the real durations, and the `agent.state_changed`
events match a live bot, so the speaking UI can be built without one.

Two things worth knowing:

- **Recall requires `automatic_audio_output` at bot creation**, or the on-demand audio
  endpoint is disabled for that bot's whole life. Every bot Kindred dispatches gets a
  silent clip in that slot to keep the path open. `--announce greeting` replaces it with
  a spoken disclosure on join.
- **`RECALL_API_KEY` is region-scoped** (`RECALL_REGION`, default `us-west-2`). A key
  from another region returns 401 on every call, which reads like a bad key.

Regenerate the sample clips — Windows SAPI where available, ffmpeg tones otherwise —
with `python scripts/make_sample_audio.py --force`.
