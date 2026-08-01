# meet_AGI

**A Google Meet copilot that actually speaks.**

The agent is called **Kindred**. It joins your meeting as a participant, hears every
speaker on a separate audio track, knows who each person is, and stays quiet.

- **Ambient mode (default).** Kindred listens for factual claims and checks them against
  your documents *and* against what people said earlier in the same meeting. When it finds
  a real conflict, it flags it in the meeting chat and puts the full reasoning — with
  citations — in the dashboard.
- **Speech mode.** Say *"Hey AGI"* and it wakes up, searches the documents, answers out
  loud in the meeting, and types the answer into the chat.

Built at the AGI House hackathon on [Inworld](https://inworld.ai) (voice),
[Character.AI](https://character.ai) (persona), and [Tenstorrent](https://tenstorrent.com)
(ambient triage inference), with [Recall.ai](https://recall.ai) for meeting I/O.

---

## Status

The API contract is live, the backend runs, and the STT → LLM → TTS loop is wired end to
end. See **[DESIGN.md](./DESIGN.md)** for the full architecture.

| Milestone | State |
|---|---|
| 0 — Schemas, OpenAPI, generated TS, fixture harness | ✅ done |
| 1 — Config CRUD (people, documents, integrations, settings) | ✅ done, in-memory |
| 2 — Harness emits the full live event stream | ✅ done |
| 3 — Document retrieval | ✅ done over `knowledge/*.txt` (no pgvector — see below) |
| 4 — Ambient loop: triage → reason → interjection | ✅ done, real Claude reasoning |
| 5 — Recall bot join + per-speaker transcript | 🟡 bot join, audio out, and chat posting done; live transcript ingestion open |
| 6 — Chat alert posting | ✅ done, server-capped at 500 chars |
| 7 — Speech mode with real TTS | ✅ done on Inworld |
| 8 — Sentence-level streaming | ⬜ open (single clip per answer today) |

**Transcript ingestion is the one gap in the live path.** Everything downstream of a
finalized utterance is real; the harness supplies those utterances today, and Recall's
transcript stream will call the same function (`app.pipeline.handle_final_segment`).

## The loop

Every finalized utterance goes through one entry point:

```
handle_final_segment(segment)
   │
   ├── "Hey AGI" heard ──▶ speech mode
   │                        retrieve → answer → Inworld TTS → speak → type into chat
   │
   └── otherwise ────────▶ ambient mode
                            triage → retrieve → find conflicts → rate-limit → type into chat
```

The ambient loop looks for two kinds of conflict: a claim that contradicts the documents,
and a claim that contradicts what someone else already said in this meeting.

**Reasoning runs on Gemini** (`gemini-3.5-flash-lite`), roughly 1-2s per call. Claude is
still wired behind the same `LLMProvider` seam — set `LLM_PROVIDER=claude` to switch.

**Kindred talks while it thinks.** Retrieval plus generation is a couple of seconds, and
to a room that just asked a question out loud, silence reads as "it didn't hear me". So
it plays a short filler first — *"Great question, on it now."* — in its own voice, then
the answer. The lines are synthesized once and cached under
`app/assets/audio/fillers/<voice>/`, so they cost nothing at wake time. Playback is
serialized per meeting, which is what guarantees the answer waits for the filler rather
than talking over it.

**Retrieval is plain text, deliberately.** `knowledge/*.txt` is chunked on `##` headings
and scored by keyword overlap, then Claude reads the top handful. The corpus is small
enough that a frontier model beats cosine similarity over it, and it keeps Postgres,
pgvector, and an embedding provider off the critical path. `app/knowledge/base.py`
`retrieve()` is the seam if that stops being true. Files map back onto the seeded
`Document` records by filename stem, so citations carry a real `document_id`.

## Quick links

- [**FRONTEND.md**](./FRONTEND.md) ← screen-by-screen spec, start here if you're building the UI
- [**frontend/README.md**](./frontend/README.md) ← frontend setup and the API layer
- [**API contract**](./DESIGN.md#8-api-contract) ← the frontend/backend boundary
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

`GET /api/health` tells you what is actually wired up — whether Kindred will reason for
real or replay fixtures, speak with Inworld or play sample clips, and how many document
chunks it loaded. Check it before blaming the demo.

### Turning the real loop on

With a reasoning key set (`GEMINI_API_KEY`, or `ANTHROPIC_API_KEY`), the harness stops
replaying its scripted conclusions and feeds the transcript to the live pipeline
instead — Kindred has to *find* the planted contradiction in `knowledge/`, not be handed
it. With no key everything still runs on canned output, so the frontend is buildable
with an empty `.env`.

```bash
cp .env.example .env      # then fill in GEMINI_API_KEY and INWORLD_API_KEY
```

The test suite pins both providers off (`tests/conftest.py`), so it behaves the same
whether or not you have keys locally.

`autonomy` controls how far an interjection travels: `silent` (dashboard only),
`propose` (waits for approval), `auto_post` (types into the meeting — the default).

```bash
curl -X PATCH localhost:8000/api/settings -H 'content-type: application/json' \
  -d '{"autonomy":"silent"}'
```

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

Real TTS runs on **Inworld** (`POST /tts/v1/voice`, mp3 out) whenever `INWORLD_API_KEY`
is set; without it, the same path plays pre-baked sample clips and flags the utterance
`placeholder: true` so the UI can be honest about it. Send a bot into a meeting and have
it talk:

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
