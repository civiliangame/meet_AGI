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
(Qwen inference — set `LLM_PROVIDER=tenstorrent`), with [Recall.ai](https://recall.ai)
for meeting I/O.

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
| 4 — Ambient loop: triage → reason → interjection | ✅ done, real reasoning on Gemini |
| 5 — Recall bot join + per-speaker transcript | ✅ done, live |
| 6 — Chat alert posting | ✅ done, server-capped at 500 chars |
| 7 — Speech mode with real TTS | ✅ done on Inworld |
| 8 — Sentence-level streaming | ⬜ open (single clip per answer today) |
| — Video tile on the bot's camera | ✅ status card, pushed on every state change |

The whole loop runs against a real Google Meet: the bot hears you, wakes on "Hey AGI",
answers out loud, and types the answer into chat. Say it and it responds in a few
seconds.

### Hearing the meeting

Recall pushes real-time transcript to this backend, so it needs a **public https URL**:

```bash
ngrok http --url=<your-static-domain> 5000     # then set PUBLIC_BASE_URL to that domain
```

Without `PUBLIC_BASE_URL` the bot joins and speaks but never hears, and the wake word
silently never fires — `GET /api/health` and a startup warning both call this out.

Recall streams transcript **word by word**, so `app/ingest/recall_live.py` buffers per
speaker and flushes an utterance on terminal punctuation, a ~1s silence gap
(`TRANSCRIPT_SILENCE_MS`), or a hard ceiling (`TRANSCRIPT_MAX_UTTERANCE_MS`). That is
what "after every person finishes speaking" actually means in practice, and it is why
wake matching sees `"Hey AGI, what does the deck say"` rather than the single word
`"hey"`. The flushed segment goes to the same `handle_final_segment` the fixture harness
calls.

The ceiling is the one that matters live. A silence gap only arrives if there is
silence, and with an open microphone there is not — breathing, keyboards, and the next
sentence all land inside the gap and reset it, so the buffer grows forever and nothing
is ever flushed. That is the "it only responds if you mute at the end" failure. Once a
wake phrase is in the buffer both windows tighten (`TRANSCRIPT_WAKE_SILENCE_MS`,
`TRANSCRIPT_WAKE_MAX_MS`), because at that point somebody is visibly waiting.

Say **"AGI, stop talking"** and it stops mid-word. That phrase is matched on *partial*
transcript, ahead of everything else: it cancels the reasoning in flight, drops the
speech queue, and retracts the clip already playing through Recall's stop-audio
endpoint. `POST /api/meetings/{id}/interrupt` is the same thing from the dashboard.

## The loop

Every finalized utterance goes through one entry point:

```
handle_final_segment(segment)
   │
   ├── "AGI stop talking" ─▶ stop. cancel the audio, the queue, and the reasoning
   │
   ├── "Hey AGI" heard ────▶ speech mode
   │                          retrieve → answer → Inworld TTS → speak → type into chat
   │
   └── otherwise ──────────▶ ambient mode
                              triage → retrieve → find a contradiction → rate-limit → chat
```

**The ambient loop only speaks up for a contradiction.** Not extra context, not a useful
qualification, not an interesting related figure — a contradiction, meaning two specific
statements that cannot both be true, each quoted verbatim: one from the documents or
from earlier in the transcript, one from the utterance under review. A verdict that
cannot produce both statements is dropped before it reaches the rate limiter, because a
model that flags a conflict it cannot quote has reasoned its way there rather than read
it off the page. Everything else stays quiet.

**Reasoning runs on Gemini** (`gemini-3.5-flash-lite`), roughly 1-2s per call. Claude is
still wired behind the same `LLMProvider` seam — set `LLM_PROVIDER=claude` to switch.

**Every chat message names its trigger.** Posts open with `Because you mentioned <topic>:`
so a line arriving 20 seconds later isn't a non-sequitur. The model supplies the topic;
the prefix is applied server-side in `app/chat/sinks.py`, so it can't end up doubled or
missing depending on how the model felt that turn.

**The bot's camera shows what Kindred is doing.** `app/video/card.py` renders a 1280x720
JPEG — agent state, the last headline, its citation — and pushes it via Recall's
`output_video` whenever state changes. Recall's other path, Output Media, streams real
MP4/GIF over a socket and is what you'd want for animation or an avatar; a replaceable
still is a fraction of the work and reads as live. The card observes the event bus rather
than being called from each site, because `agent.state_changed` is published from three
places and wiring only one leaves the tile stuck on `thinking`.

**Kindred talks while it thinks.** Retrieval plus generation is a couple of seconds, and
to a room that just asked a question out loud, silence reads as "it didn't hear me". So
it plays a short filler first — *"Great question, on it now."* — in its own voice, then
the answer. The lines are synthesized once and cached under
`app/assets/audio/fillers/<voice>/`, so they cost nothing at wake time. Playback is
serialized per meeting, which is what guarantees the answer waits for the filler rather
than talking over it.

**Retrieval is plain text, deliberately.** `knowledge/*.txt` is chunked on `##` headings
and scored by keyword overlap, then the model reads the top handful. The corpus is small
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

One command puts Kindred in the default meeting, starting the backend if it isn't up:

```bash
python scripts/kindred.py            # backend + dashboard + bot, after a preflight
python scripts/kindred.py --watch    # ...and tail what it hears and says
python scripts/kindred.py --no-ui    # backend and bot only
python scripts/kindred.py --check    # preflight only, dispatch nothing
python scripts/kindred.py --leave    # pull the bot out
python scripts/kindred.py <meet-url> # a different meeting
```

That is backend on `:5000`, dashboard on `:3000`, and Kindred in the meeting. It also
writes `frontend/.env.local` so the dashboard points at whichever port the backend is
actually on — the frontend defaults to `:8000`, so without that the dashboard loads,
looks entirely healthy, and shows nothing.

Unlike the backend, an already-running `next dev` is left alone: Next hot-reloads, so it
is never serving a stale build. The one thing it will not pick up is `.env.local`, which
is read at startup, so the script warns when it has just changed one under a running
dev server.

**It always restarts the backend.** Reusing a server that is already listening means a
code change silently does not take effect, and the symptom is a feature that "doesn't
work" while the old build quietly serves. Before killing it, the script asks that server
to pull its bots out of their meetings — a Recall bot is a server-side entity, so killing
the process that dispatched it only orphans it in the call until Recall's timeout. After
the restart it sweeps Recall for any bot a previous crash left behind.

It refuses to kill a process whose command line is not a Kindred server: port 5000 is
also home to Flask and macOS AirPlay Receiver, and taking one of those down because it
happens to hold the port is worse than stopping. `--force` overrides.

The preflight refuses to dispatch when something is actually broken — most usefully it
round-trips `PUBLIC_BASE_URL` to prove the tunnel is *up*, not merely configured. A dead
tunnel produces a bot that joins, speaks, and never hears, which is indistinguishable
from a working one until someone says the wake word.

### Running the pieces by hand

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

`LLM_PROVIDER` picks the reasoning backend: `auto` (Gemini, then Claude, then
Tenstorrent), or force one with `gemini`, `claude`, `tenstorrent`, or `none`. Setting
`LLM_PROVIDER=tenstorrent` routes both loops to `Qwen/Qwen3-32B` on Tenstorrent hardware
— no code change, and `GET /api/health` reports which one is actually live. A
`TENSTORRENT_API_KEY` on its own will not take over from Gemini; it sits last in the
`auto` chain, so flipping the flag is the deliberate act.

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

And one for Tenstorrent, if you switch to it:

- **Do not "upgrade" `TENSTORRENT_MODEL` to `Qwen/Qwen3-VL-32B-Instruct`.** It is the
  newer model in the catalogue and it accepts `response_format` and then ignores it —
  HTTP 200, wrong JSON shape, no error anywhere. Every reasoning call here is
  schema-constrained. `Qwen/Qwen3-32B` enforces the schema.

Regenerate the sample clips — Windows SAPI where available, ffmpeg tones otherwise —
with `python scripts/make_sample_audio.py --force`.
