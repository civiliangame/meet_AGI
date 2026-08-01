# meet_AGI — Design Doc

**A Google Meet copilot that actually speaks.**

Codename for the agent: **Kindred**.

---

## 1. What it is

Kindred joins a Google Meet as a participant. It hears every speaker on a separate
audio track, knows who each person is, and stays quiet by default.

It has two loops:

**Ambient loop (silent, always on).** Kindred listens for factual claims, checks them
against the documents and context you gave it, and when it finds a real conflict it
drops a one-line flag into the meeting chat with the full reasoning in the web app.

**Speech mode (on demand).** Say "Kindred" and it wakes up, listens to your question,
asks a clarifying question if the question is ambiguous, and answers out loud in the
meeting.

The difference from every other notetaker: it participates.

---

## 2. Confirmed platform capabilities

Meeting ingestion runs on **Recall.ai**. Verified against their docs (2026-08-01):

| Requirement | Google Meet | Mechanism |
|---|---|---|
| Bot joins meeting | ✅ | `POST /api/v1/bot` with `meeting_url` |
| Per-speaker separated audio, real-time | ✅ (16 concurrent speakers) | `recording_config.audio_separate_raw = {}` + websocket subscribed to `audio_separate_raw.data`. Mono 16-bit signed LE PCM @ 16 kHz |
| Real-time transcript with speaker attribution | ✅ | `transcript.data` events; built-in STT or Deepgram/AssemblyAI/ElevenLabs |
| Bot speaks into meeting | ✅ | Output Media API (Recall's recommended path for conversational agents) |
| Bot posts to in-meeting chat | ✅ | `POST /bot/{id}/send_chat_message`, supports pinning |
| Participant join/leave/speaking events | ✅ | `participant_events` |

**Cost:** $0.50/hr of recording, prorated to the second. First 5 hours free. Built-in
transcription +$0.15/hr. Budget for the whole hackathon including heavy iteration: under $20.

### Constraints that shaped this design

1. **Google Meet chat has a 500-character limit.** This is the single biggest product
   constraint. It is why chat is an *alert channel only* — see §4.
2. **Real-time separate-audio streams exclude screenshare audio.** If someone plays a
   video or shares a tab with sound, Kindred is deaf to it live. It appears in the
   post-call recording only. Accepted limitation.
3. **Separate audio is compute-heavy — Recall recommends 4-core bots.** One config flag,
   but streams drop silently if you miss it.
4. **Output Media's exact streaming interface needs nailing down in implementation.**
   Capability is confirmed; the chunk-level streaming contract is not yet verified.
   v1 ships on the simpler `output_audio` endpoint (complete mp3 as base64) and upgrades
   to streamed Output Media only if latency demands it. See §7 for the latency budget.

---

## 3. Architecture

```mermaid
flowchart TB
    GM["Google Meet"]
    RC["Recall.ai bot<br/>(4-core)"]
    HARNESS["Dev harness<br/>(fixture replay)"]

    subgraph BE["meet_AGI backend — FastAPI"]
        ING["ingest/<br/>normalize events"]
        IDENT["identity/<br/>participant → Person"]
        BUS["event bus"]
        WAKE["wake/<br/>wake-word detect"]
        TRIAGE["triage/<br/>is this a checkable claim?"]
        RAG["rag/<br/>pgvector retrieval"]
        REASON["reason/<br/>Claude contradiction + answer"]
        VOICE["voice/<br/>Inworld TTS"]
        API["api/<br/>REST + WS"]
    end

    DB[("Postgres 16<br/>+ pgvector")]
    FE["Next.js frontend"]

    GM <--> RC
    RC -->|"WS: audio_separate_raw,<br/>transcript, participants"| ING
    HARNESS -.->|"same event schema"| ING
    ING --> IDENT --> BUS
    BUS --> WAKE
    BUS --> TRIAGE
    TRIAGE --> RAG --> REASON
    WAKE --> REASON
    REASON --> VOICE
    VOICE -->|"mp3 b64"| RC
    REASON -->|"chat alert ≤500ch"| RC
    REASON --> DB
    RAG <--> DB
    BUS --> API
    API <-->|"REST + WebSocket"| FE
```

### Why a dev harness is a first-class component

You cannot iterate on reasoning quality by hosting a live Google Meet every time. The
`ingest/` layer accepts events from either Recall or a **fixture replay harness** that
emits the identical normalized event schema from a scripted multi-speaker transcript.

This means:
- Your partner develops the entire frontend without ever touching Recall or a real meeting.
- Reasoning-quality iteration costs zero dollars and zero coordination.
- You get a rehearsed, deterministic demo path if conference wifi betrays you on stage.

Fixtures live in `fixtures/meetings/*.jsonl`. Ship at least one — `q3_revenue_review` —
containing a planted contradiction and a planted "Kindred, ..." wake event.

---

## 4. The ambient loop

Runs on every finalized utterance.

1. **Triage** — does this utterance contain a checkable factual assertion? High-volume,
   small-model job. Runs on **Tenstorrent** when available (see §6). Cheap heuristic
   prefilter first: skip utterances under ~8 words, skip pure back-channel ("yeah", "right").
2. **Retrieve** — pgvector similarity over document chunks, filtered by tags, top-k 8.
3. **Reason** — Claude evaluates: does the retrieved evidence contradict, complicate, or
   materially qualify the claim? Returns a structured verdict with confidence and citations.
4. **Gate** — emit only if `confidence >= min_confidence` AND `cooldown_seconds` elapsed
   since the last interjection AND `max_per_meeting` not hit.
5. **Emit** — two artifacts from one interjection:
   - **`chat_alert`** → posted to Meet chat. Hard-capped at 500 chars, targeting ~200.
     A flag, not an argument. No link (a URL in Meet chat is noise and unclickable-ugly).
   - **`headline` + `body_md` + `citations`** → pushed to the frontend live view over
     WebSocket. This is where the actual reasoning lives.

**The rate limiter is a feature, not an optimization.** A copilot that won't shut up is
worse than no copilot. Defaults: `min_confidence 0.7`, `cooldown 90s`, `max 8 per meeting`.

**Autonomy levels** (`settings.autonomy`):
- `silent` — nothing posted to the meeting; frontend only. Safest demo default.
- `propose` — interjections appear in frontend as `proposed`, a human clicks approve, then it posts.
- `auto_post` — posts to chat immediately. The real product; the impressive demo.

Example `chat_alert` (187 chars):

```
⚠️ Kindred: revenue claim conflicts with Q3 deck (p.14 shows new-product
line down 12% MoM, not up). Likely gross vs. net. Full analysis in the
Kindred dashboard.
```

---

## 5. Speech mode

State machine on `agent_state`: `idle → listening → thinking → speaking → idle`,
plus `muted` as a hard override.

1. **Wake detection.** Match the wake word on *finalized* transcript segments only —
   partials produce false triggers. Require the wake word at utterance start, or
   immediately followed by question-shaped continuation. Debounce 3s.

   False positives are the real risk here ("kindred spirits", or people *discussing*
   Kindred during the demo). Mitigations:
   - A confirmation state: Kindred plays a short ack chime, doesn't speak until it has a question.
   - A **manual wake button** in the frontend. Non-negotiable demo safety net.
   - A **mute kill switch** in the frontend. Also non-negotiable.

2. **Capture question.** Accumulate finalized segments from the waking speaker until
   1.5s of silence or 20s hard cap.

3. **Clarify (at most once).** If the question is ambiguous or underspecified, generate
   exactly one clarifying question, speak it, and return to listening. Cap at one round —
   a bot that interrogates you is a bad demo.

4. **Answer.** RAG retrieve → Claude generates → Character.AI persona layer shapes the
   phrasing (if enabled) → Inworld TTS → mp3 → Recall.

5. **Record.** The spoken answer is persisted as an `Interjection` with `kind: "answer"`
   and `spoken: true`, so the frontend timeline shows everything Kindred said and why.

---

## 6. Sponsor integration

All three sit behind provider interfaces so any one can be swapped or dropped without
touching the pipeline. **Inworld is load-bearing** (voice is the entire premise of
"actually speaks"). The other two are real but droppable under time pressure.

### Inworld — voice. Load-bearing.
`VoiceProvider.synthesize(text, voice_id) -> mp3 bytes`

Kindred's speaking voice. This is the sponsor tech doing the thing the demo is named
after. Fallback implementation: ElevenLabs, or OS TTS for offline dev.

### Character.AI — persona. Genuinely useful.
`PersonaProvider.shape(draft_text, context) -> str`

Claude does the reasoning; Character.AI gives Kindred a consistent character — tone,
verbal tics, how it hedges, how it interrupts politely. The separation matters: you do
not want persona bleeding into analytical accuracy. Reason first, then shape.
Fallback: a Claude system prompt.

### Tenstorrent — ambient triage. The honest fit.
`TriageProvider.is_checkable_claim(utterance, context) -> (bool, float)`

This is the highest-QPS model call in the system — it runs on *every* utterance in
*every* meeting, forever. It's small, it's classification, and it never stops. That is
exactly the workload that justifies dedicated inference hardware, and there's a real
cost argument: you should not pay frontier-model prices to decide whether "yeah, sounds
good" is worth fact-checking.

Fallbacks: `claude` (Haiku) or `heuristic` (keyword + POS rules). **This is the first
thing to cut if you run short on time** — set `triage.provider: "heuristic"` and the
demo still works end to end.

**Open question for you:** do you have Tenstorrent cloud credentials yet? Getting a model
serving on their hardware is a half-day minimum and it's the only sponsor item on the
critical path for setup rather than code.

---

## 7. Latency budget for speech mode

This is what separates "impressive" from "awkward." Measured from question-end to
first audio in the meeting:

| Stage | Expected |
|---|---|
| Recall transcript finalize → backend | 300–800 ms |
| RAG retrieve (pgvector, top-k 8) | ~100 ms |
| Claude first sentence (streamed) | ~600 ms |
| Inworld TTS first chunk | 200–500 ms |
| Recall Output Media → audible in meeting | 500–1000 ms |
| **Total to first audio** | **~2–3 s** |

**The design requirement that falls out of this: stream at sentence granularity.**
Generate the answer sentence-by-sentence, TTS each sentence as it completes, and push
audio while later sentences are still being written. If you wait for the full answer
before synthesizing, first-audio lands at 5–7s and it feels broken.

v1 may ship non-streaming (complete mp3 via `output_audio`) to get end-to-end working.
Treat streaming as a required upgrade before demo, not a nice-to-have.

---

## 8. API contract

**This section is the coordination boundary.** Your partner builds against this without
reading backend code.

**Source of truth:** Pydantic v2 models in `backend/app/schemas/`. FastAPI generates
OpenAPI; `scripts/gen-types.sh` runs `openapi-typescript` into
`frontend/src/lib/api/generated.ts`. That file is generated — never hand-edited. Run the
script after any schema change and commit the result, so the frontend always has types
even when the backend isn't running.

**Conventions:**
- Base path `/api`. JSON everywhere except file upload (multipart).
- IDs are prefixed ULIDs: `prs_`, `doc_`, `mtg_`, `seg_`, `itj_`, `chk_`.
- Timestamps are RFC 3339 UTC with milliseconds.
- Errors: `{"error": {"code": "not_found", "message": "...", "detail": {...}}}`
  with conventional HTTP status codes.
- Lists are `{"items": [...], "next_cursor": "..." | null}`.
- **All mutations go over REST. The WebSocket is server→client only** (except `ping`).
  One write path, no dual-write bugs.

### 8.1 Core objects

```jsonc
// Person — someone who attends meetings
{
  "id": "prs_01J8XK2M3N4P5Q6R7S8T9V",
  "display_name": "Sarah Chen",
  "aliases": ["Sarah", "S. Chen"],
  "role": "VP Finance",
  "org": "Acme Corp",
  "email": "sarah@acme.com",
  "bio": "Owns the quarterly revenue model. Presents the board deck.",
  "voice_sample_url": null,
  "created_at": "2026-08-01T17:04:11.221Z",
  "updated_at": "2026-08-01T17:04:11.221Z"
}
```

```jsonc
// Document — uploaded or integration-sourced context
{
  "id": "doc_01J8XK4A...",
  "filename": "Q3-board-deck.pdf",
  "mime_type": "application/pdf",
  "size_bytes": 284119,
  "source": "upload",              // upload | slack | gmail | gdrive | notion
  "status": "ready",               // pending | parsing | embedding | ready | failed
  "error": null,
  "chunk_count": 84,
  "tags": ["finance", "q3"],
  "created_at": "2026-08-01T17:09:02.010Z"
}
```

```jsonc
// Integration — stubbed for the hackathon
{
  "provider": "slack",             // slack | gmail | gdrive | notion | salesforce
  "display_name": "Slack",
  "status": "connected",           // available | connected | error
  "connected_at": "2026-08-01T17:12:44.000Z",
  "account_label": "acme.slack.com",
  "capabilities": ["documents", "messages"],
  "is_stub": true                  // frontend shows a "Demo" badge when true
}
```

`is_stub` exists so the UI can be honest without the frontend hardcoding which providers
are fake. When a connection becomes real, the flag flips and the badge disappears.

```jsonc
// Settings — singleton
{
  "wake_word": "Kindred",
  "wake_word_enabled": true,
  "autonomy": "auto_post",         // silent | propose | auto_post
  "interjection": {
    "min_confidence": 0.7,
    "cooldown_seconds": 90,
    "max_per_meeting": 8
  },
  "voice": {
    "provider": "inworld",         // inworld | elevenlabs | system
    "voice_id": "kindred_v1",
    "speaking_rate": 1.0
  },
  "persona": {
    "provider": "characterai",     // characterai | claude
    "character_id": null,
    "tone": "concise_analyst"
  },
  "triage": {
    "provider": "tenstorrent"      // tenstorrent | claude | heuristic
  }
}
```

```jsonc
// Meeting
{
  "id": "mtg_01J8XM...",
  "title": "Q3 Revenue Review",
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "platform": "google_meet",
  "state": "in_call",              // scheduled | joining | in_call | ended | failed
  "agent_state": "idle",           // idle | listening | thinking | speaking | muted
  "source": "recall",              // recall | harness
  "bot_id": "8f2a1c94-...",        // Recall bot id; null when source=harness
  "roster": [
    {
      "participant_id": "p_2",     // platform-scoped, from Recall
      "person_id": "prs_01J8XK2M...",  // null when unmatched
      "display_name": "Sarah Chen",
      "is_host": false,
      "matched": true,
      "is_speaking": false
    }
  ],
  "started_at": "2026-08-01T18:00:03.100Z",
  "ended_at": null,
  "stats": {
    "utterance_count": 412,
    "interjection_count": 3,
    "duration_seconds": 1840
  },
  "error": null
}
```

```jsonc
// TranscriptSegment
{
  "id": "seg_01J8XN...",
  "meeting_id": "mtg_01J8XM...",
  "participant_id": "p_2",
  "person_id": "prs_01J8XK2M...",  // nullable
  "speaker_name": "Sarah Chen",
  "text": "new product revenue is up about eight percent this quarter",
  "is_final": true,
  "start_ms": 154200,
  "end_ms": 158900,
  "confidence": 0.94
}
```

```jsonc
// Interjection — the central object. Everything Kindred says or wants to say.
{
  "id": "itj_01J8XP...",
  "meeting_id": "mtg_01J8XM...",
  "kind": "contradiction",         // contradiction | context | correction | answer | clarification
  "status": "posted",              // proposed | approved | posted | dismissed | failed
  "trigger": {
    "segment_ids": ["seg_01J8XN..."],
    "person_id": "prs_01J8XK2M...",
    "quote": "new product revenue is up about eight percent this quarter"
  },
  "chat_alert": "⚠️ Kindred: revenue claim conflicts with Q3 deck (p.14 shows new-product line down 12% MoM, not up). Likely gross vs. net. Full analysis in the Kindred dashboard.",
  "headline": "Sarah's revenue claim conflicts with the Q3 board deck",
  "body_md": "Sarah stated new-product revenue is **up ~8%** this quarter.\n\nThe Q3 board deck (p.14) shows the new-product line at **-12% MoM**...",
  "confidence": 0.82,
  "citations": [
    {
      "document_id": "doc_01J8XK4A...",
      "filename": "Q3-board-deck.pdf",
      "chunk_id": "chk_01J8XK5B...",
      "page": 14,
      "quote": "New Product Line: $1.42M (-12.1% MoM)",
      "relevance": 0.91
    }
  ],
  "spoken": false,
  "created_at": "2026-08-01T18:22:04.118Z",
  "posted_at": "2026-08-01T18:22:05.402Z"
}
```

`chat_alert` is server-enforced ≤500 chars. The frontend can render it verbatim as a
preview of what the meeting saw.

### 8.2 REST endpoints

**People**
```
GET    /api/people                     → {items, next_cursor}
POST   /api/people                     ← {display_name, role?, org?, email?, bio?, aliases?}
GET    /api/people/{id}
PATCH  /api/people/{id}
DELETE /api/people/{id}
POST   /api/people/{id}/voice-sample   ← multipart (audio) → Person
```

**Documents**
```
GET    /api/documents                  → {items, next_cursor}   ?status= ?tag= ?source=
POST   /api/documents                  ← multipart file[] + tags[] → {items: Document[]}
GET    /api/documents/{id}
DELETE /api/documents/{id}
```
Upload returns immediately with `status: "pending"`. Progress arrives via
`GET /api/documents` polling, or the global WS if the frontend is connected.

**Integrations**
```
GET    /api/integrations                        → {items: Integration[]}
POST   /api/integrations/{provider}/connect     ← {} → Integration
DELETE /api/integrations/{provider}             → Integration
```
For the hackathon, `connect` flips `status` to `connected` after a ~1.2s simulated delay
and sets a plausible `account_label`. No OAuth. The endpoint shape is the real one, so
wiring a genuine integration later touches only the backend.

**Settings**
```
GET    /api/settings                   → Settings
PATCH  /api/settings                   ← partial Settings → Settings
```

**Meetings**
```
GET    /api/meetings                   → {items, next_cursor}   ?state=
POST   /api/meetings                   ← {meeting_url, title?, expected_person_ids?[]}  → Meeting
GET    /api/meetings/{id}              → Meeting
POST   /api/meetings/{id}/leave        → Meeting
GET    /api/meetings/{id}/transcript   → {items: TranscriptSegment[], next_cursor}  ?cursor= ?limit=
GET    /api/meetings/{id}/interjections → {items: Interjection[], next_cursor}
```

**Agent control** — the demo safety net
```
POST   /api/meetings/{id}/wake         ← {}                    → Meeting   // manual wake
POST   /api/meetings/{id}/mute         ← {muted: bool}         → Meeting   // kill switch
POST   /api/meetings/{id}/ask          ← {question, speak?: bool}  → Interjection
```
`/ask` lets you type a question from the dashboard and optionally have Kindred speak the
answer into the meeting. This is the single most valuable demo-recovery tool in the API —
if wake-word detection misfires on stage, you type the question and it still speaks.

**Interjection review** — used when `autonomy: "propose"`
```
POST   /api/interjections/{id}/approve  ← {edited_chat_alert?: string} → Interjection
POST   /api/interjections/{id}/dismiss  ← {reason?: string}            → Interjection
POST   /api/interjections/{id}/speak    ← {}                           → Interjection
```

**Dev harness** — how the frontend is built without a live meeting
```
GET    /api/dev/fixtures               → {items: [{id, title, duration_seconds, description}]}
POST   /api/dev/harness/start          ← {fixture_id, speed?: 1.0}  → Meeting
POST   /api/dev/harness/stop           ← {meeting_id}               → Meeting
```
Creates a `Meeting` with `source: "harness"` that emits the full event stream —
transcript, participants, interjections, wake events — indistinguishable from a real
meeting to any WS consumer. `speed: 4.0` fast-forwards for quick iteration.

### 8.3 WebSocket

```
WS /api/meetings/{id}/live      — per-meeting event stream
WS /api/live                    — global stream (document status, meeting lifecycle)
```

Every frame shares one envelope:

```jsonc
{
  "type": "transcript.final",
  "seq": 1428,                   // monotonic per connection; detect gaps
  "meeting_id": "mtg_01J8XM...",
  "ts": "2026-08-01T18:22:04.118Z",
  "data": { /* shape determined by type */ }
}
```

On connect the server sends exactly one `snapshot` frame carrying full current state, so
the frontend never needs a REST call to initialize the live view. Reconnect with
`?since_seq=N` to replay missed frames (buffer: last 500).

| `type` | `data` |
|---|---|
| `snapshot` | `{meeting: Meeting, recent_segments: TranscriptSegment[], interjections: Interjection[]}` |
| `meeting.state_changed` | `{state, agent_state, error?}` |
| `participant.joined` | `{participant: RosterEntry}` |
| `participant.left` | `{participant_id}` |
| `participant.speaking_changed` | `{participant_id, is_speaking}` |
| `transcript.partial` | `TranscriptSegment` (`is_final: false`) |
| `transcript.final` | `TranscriptSegment` (`is_final: true`) |
| `interjection.proposed` | `Interjection` |
| `interjection.updated` | `Interjection` (status transitions) |
| `agent.state_changed` | `{agent_state, detail?}` |
| `speech.wake_detected` | `{participant_id, person_id?, segment_id, matched_text}` |
| `speech.question_captured` | `{question, segment_ids}` |
| `speech.clarification_asked` | `{question}` |
| `speech.answered` | `{interjection_id}` |
| `document.status_changed` | `Document` (global stream only) |
| `error` | `{code, message}` |

`transcript.partial` frames are high-frequency. The frontend should render them into a
single mutable "live line" per speaker and only commit to the transcript log on
`transcript.final`.

### 8.4 Contract discipline

- Backend owns `backend/app/schemas/`. Frontend never defines an API type by hand.
- Any schema change: run `scripts/gen-types.sh`, commit the generated file, note it in
  the PR description.
- **Additive changes are free. Removals and renames need a heads-up in Slack.** With two
  people moving fast in parallel, this is the rule that prevents a lost afternoon.
- Enum values are closed sets. Frontend switch statements should have a `default` branch
  rather than assuming exhaustiveness — the backend will add variants.

---

## 9. Frontend spec

Two surfaces.

### Config (`/settings`)
- **People** — CRUD list. Name, role, org, email, bio, aliases, optional voice sample.
  This is how Kindred knows who's talking and why they'd say it.
- **Documents** — drag-drop upload, per-file status chip (pending → parsing → embedding →
  ready), tag editor, delete.
- **Integrations** — card grid: Slack, Gmail, Google Drive, Notion, Salesforce. Connect
  button, connected state, "Demo" badge driven by `is_stub`.
- **Agent** — wake word, autonomy selector (with plain-language descriptions of each
  level, because `auto_post` vs `propose` is a trust decision), confidence threshold,
  cooldown, voice picker, persona tone.

### Live meeting view (`/meetings/[id]`)
The demo screen. Everything here is WS-driven.

- **Header** — meeting title, elapsed time, agent state pill (`idle`/`listening`/
  `thinking`/`speaking`/`muted`) with distinct colors. This pill is the whole demo:
  the audience watches it flip to `listening` the instant someone says "Kindred."
- **Roster rail** — participants with live speaking indicator, matched Person name and
  role, unmatched participants visually flagged.
- **Transcript** — auto-scrolling, speaker-colored, partials rendered as a dim live line
  that solidifies on final.
- **Interjection feed** — the payoff. Each card: headline, confidence, the triggering
  quote, full `body_md`, expandable citations with document name and page. When
  `autonomy: "propose"`, approve/dismiss buttons. Shows the exact `chat_alert` that went
  into the meeting.
- **Controls** — mute kill switch, manual wake button, and an ask box (types straight to
  `POST /ask`). All three are stage-insurance.

Design note: the interjection card is the thing people screenshot. It carries the
argument that chat's 500 characters can't. Give it the most design attention.

---

## 10. Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.11, FastAPI, Pydantic v2, uvicorn | Free OpenAPI generation; the whole ML ecosystem |
| DB | Postgres 16 + pgvector | Real RAG over uploaded docs; one container |
| Frontend | Next.js 15 (App Router), TypeScript, Tailwind | Partner's independent surface |
| Contract | OpenAPI → `openapi-typescript` | Generated types, never hand-synced |
| Reasoning | Claude (Opus for interjections, Haiku for triage fallback) | Quality where it matters, cheap where it doesn't |
| Voice | Inworld | Sponsor, load-bearing |
| Persona | Character.AI | Sponsor, adapter |
| Triage | Tenstorrent | Sponsor, adapter |
| Meeting I/O | Recall.ai | Verified capability matrix, §2 |
| Local dev | docker-compose (postgres) + `make dev` | Partner runs one command |

### Repo layout

```
meet_AGI/
├── DESIGN.md
├── README.md
├── docker-compose.yml
├── Makefile
├── .env.example
├── backend/
│   ├── pyproject.toml
│   └── app/
│       ├── main.py
│       ├── config.py
│       ├── bus.py                 # in-process pub/sub
│       ├── api/                   # routers, one per resource
│       ├── schemas/               # Pydantic — CONTRACT SOURCE OF TRUTH
│       ├── models/                # SQLAlchemy
│       ├── ingest/
│       │   ├── recall.py          # bot lifecycle, WS consumer
│       │   ├── harness.py         # fixture replay
│       │   └── normalize.py       # → internal event schema
│       ├── pipeline/
│       │   ├── ambient.py         # triage → retrieve → reason → gate
│       │   ├── speech.py          # wake → capture → clarify → answer
│       │   └── identity.py        # participant → Person matching
│       ├── rag/                   # chunk, embed, retrieve
│       └── providers/
│           ├── voice/             # inworld, elevenlabs, system
│           ├── persona/           # characterai, claude
│           ├── triage/            # tenstorrent, claude, heuristic
│           └── llm/               # claude client
├── frontend/
│   ├── package.json
│   └── src/
│       ├── app/
│       ├── components/
│       └── lib/api/
│           ├── generated.ts       # GENERATED — do not edit
│           └── ws.ts              # typed WS client
├── fixtures/meetings/
│   └── q3_revenue_review.jsonl
└── scripts/
    ├── gen-types.sh
    └── seed.py
```

### Environment

```bash
RECALL_API_KEY=
RECALL_REGION=us-west-2
ANTHROPIC_API_KEY=
INWORLD_API_KEY=
CHARACTERAI_API_KEY=          # optional
TENSTORRENT_ENDPOINT=         # optional
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/meet_agi
PUBLIC_WS_URL=               # tunnel URL Recall posts events to (ngrok in dev)
```

**Note:** Recall needs a publicly reachable `wss://` endpoint to deliver real-time
events. In dev that means ngrok or Cloudflare Tunnel. This is a real setup step and a
common source of "why is nothing arriving" — the harness exists partly so it isn't
blocking.

---

## 11. Build order

Sequenced so the frontend is unblocked at hour one.

| # | Milestone | Owner | Unblocks |
|---|---|---|---|
| 0 | Schemas + OpenAPI + generated TS + harness fixture. **No real integrations.** | Backend | **Everything on the frontend** |
| 1 | Config CRUD (people, documents, integrations stub, settings) | Backend | Config UI |
| 2 | Harness replay emitting the full WS event stream | Backend | Live view UI, built against fake data |
| 3 | Document ingestion → chunk → embed → pgvector retrieval | Backend | Real citations |
| 4 | Ambient loop: triage → reason → Interjection (autonomy `silent`) | Backend | Interjection feed |
| 5 | Recall bot join + per-speaker transcript, replacing harness | Backend | Real meetings |
| 6 | Chat alert posting (autonomy `auto_post`) | Backend | In-meeting proof |
| 7 | Speech mode: wake → capture → Inworld TTS → Recall output | Backend | **The money shot** |
| 8 | Sentence-level streaming for speech mode | Backend | Latency that feels alive |
| 9 | Clarifying questions | Backend | Nice-to-have |
| — | Config UI | Frontend | after #0 |
| — | Live meeting view | Frontend | after #0, refined at #2 |
| — | Interjection card polish | Frontend | after #2 |

**Milestone 0 is the critical path for two people.** Until schemas and generated types
land, your partner is blocked. Do it first, commit the generated TypeScript, and push
before anything else.

**Cut list under time pressure, in order:** #9 clarifying questions → Tenstorrent triage
(fall back to `heuristic`) → Character.AI persona (fall back to Claude prompt) → #8
streaming. Do not cut #7; it's the entire pitch.

---

## 12. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Wake word false-positives during the demo | **High** — someone on stage will say "Kindred" while explaining Kindred | Manual wake button, mute switch, ack chime before speaking, final-transcript-only matching |
| Recall needs a public wss:// endpoint | Medium | ngrok set up early; harness makes it non-blocking |
| Speech-mode latency feels dead | Medium | Sentence-level streaming (#8); measure first-audio explicitly |
| Kindred interjects too much | Medium | Cooldown + confidence + max-per-meeting, all tunable live from the UI |
| Tenstorrent setup eats a day | Medium | It's a swappable adapter behind `TriageProvider`; `heuristic` fallback ships day one |
| 500-char chat limit makes alerts feel thin | Low-Medium | Accepted by design: chat alerts, frontend argues |
| Screenshare audio invisible in real time | Low | Documented limitation; don't demo a screenshared video |
| Conference wifi | Medium | Harness fixture gives a fully offline demo path |

---

## 13. Open questions

1. **Tenstorrent credentials** — do you have them? It's the only sponsor item whose
   critical path is account setup rather than code.
2. **Whose Google account joins?** Recall bots can join as a guest awaiting admission, or
   you can admit them manually. For the demo, manual admission is fine and simpler.
3. **Consent/disclosure** — Recall supports pinning a chat message on join. Recommend
   pinning "Kindred is recording and may comment" by default. Cheap, and it preempts the
   obvious judge question about recording people.
4. **Do you want persistent cross-meeting memory?** Everything above is per-meeting plus
   static documents. "Kindred remembers what you said three meetings ago" is a strong
   demo beat but a schema addition — worth deciding now, not later.
