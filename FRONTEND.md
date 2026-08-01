# Kindred dashboard — frontend design doc

**Audience: the engineer (or Claude Code instance) building the frontend.** Everything
here is implementable today against a running backend. No feature described below is
blocked on backend work.

Read [`frontend/README.md`](./frontend/README.md) first for setup. This doc is the
product and screen spec.

---

## 1. What this product is

**A place for a human to check, after a meeting, what Kindred said and what it read
before saying it.**

Kindred is an AI that sits in a Google Meet, fact-checks claims against your documents,
and speaks up. That is a strong claim to make about someone's revenue numbers in front of
their CEO. So the dashboard's job is not to look impressive — it is to make Kindred
**auditable**.

Every screen should serve one question: *can I verify this?*

The single most important interaction in the product:

> Kindred said Marcus's revenue number conflicts with the Q3 deck.
> **Show me exactly where Marcus said it, and exactly what the deck says.**

If that takes more than one click in either direction, the design has failed.

### Live monitoring is secondary

There is a live mode, and the demo uses it. But a live view is a thing you glance at,
while the review view is a thing you *read*. Build review first and build it properly;
live mode is the same screen with a socket attached (§5).

---

## 2. What already exists — do not rebuild it

`frontend/src/lib/api/` is written, typechecked, and ready:

| File | Use it for |
|---|---|
| `generated.ts` | **Generated. Never edit.** Output of `./scripts/gen-types.sh`. |
| `types.ts` | Readable type aliases. **Import types from here.** |
| `client.ts` | Typed REST client (`api.meetings.bundle(id)`, …). Throws `ApiError`. |
| `ws.ts` | Typed socket with reconnect, `since_seq` replay, gap recovery. |

**Never hand-write an API type.** Everything derives from the backend's Pydantic schemas
through OpenAPI. If a type looks wrong, the backend model is wrong — say so, and it gets
fixed at the source.

You choose the app framework, styling, and data-fetching library. Next.js App Router +
Tailwind is assumed below but nothing depends on it. Scaffold in place without clobbering
`src/lib/api/`:

```bash
cd frontend
npx create-next-app@latest . --ts --tailwind --app --src-dir --no-git
```

---

## 3. Getting real data in 30 seconds

No database, no API keys, no internet, no Google account.

```bash
cd backend && ./.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

```bash
curl -X POST localhost:8000/api/dev/harness/start \
  -H 'content-type: application/json' \
  -d '{"fixture_id":"q3_revenue_review","speed":50}'
```

At `speed: 50` a full session completes in about 3 seconds and is then **persisted to
disk** — so you get a real, reviewable session to build against, and it survives a
backend restart. Run it a few times to populate the session list.

For live-mode work use `{"speed": 6, "loop": true}` to get a continuously replaying
meeting.

`POST /api/dev/reset` clears everything. Swagger UI is at http://localhost:8000/docs.

---

## 4. Screens

```
/                      Session list          — the home screen
/sessions/[id]         Session detail        — review, or live if the meeting is running
/settings              Configuration         — people, documents, integrations, agent
```

Three routes. Resist adding more.

### 4.1 Session list — `/`

Rows of past meetings, newest first.

```ts
const { items } = await api.meetings.list();
```

Returns `Meeting[]`. Everything a row needs is already on it — no per-row fetches.

Each row shows:

| Field | From |
|---|---|
| Title | `title` |
| When | `started_at` — relative ("2 hours ago") with absolute on hover |
| Duration | `stats.duration_seconds` |
| Participants | `roster[].display_name` — avatars or initials, overflow as "+2" |
| Claims made | `stats.interjection_count` |
| Sources used | `stats.source_document_count` |
| Status | `state` — only surface `failed`; `ended` is the normal case and needs no badge |

**Do not** show `agent_state` here. It is meaningless for a finished meeting.

Flag rows where `source === "harness"` discreetly — during development every session is a
fixture, and you will want to tell them apart from real ones at a glance.

Empty state matters: a first-run user sees this screen with nothing on it. Say what to do
("No sessions yet — Kindred will appear here after its first meeting"), and in dev, a
button that calls `api.dev.startHarness("q3_revenue_review", 50)` earns its place.

Deleting: `api.meetings.remove(id)` — irreversible, so confirm.

### 4.2 Session detail — `/sessions/[id]` — **the main screen**

One call gets everything:

```ts
const bundle = await api.meetings.bundle(id);
// { meeting, transcript, interjections, sources }
```

`MeetingBundle` exists specifically so this page renders without waterfalling four
requests. Use it.

**Header** — title, date, duration, participant list (with roles from the resolved
`Person`), and `meeting_url` as a link if present.

**Body — three views over one session.** Tabs, panes, or columns; your call. What matters
is that they cross-link.

---

#### View A — Claims (default)

The list of `interjections`. This is what Kindred *did*, and it is what people came to
look at, so it opens first.

Each card:

- **`headline`** — the claim, one line, prominent.
- **`kind`** badge — `contradiction` · `context` · `correction` · `answer` ·
  `clarification`. These mean different things and should look different. A
  `contradiction` is Kindred disagreeing with a human; an `answer` is Kindred being
  asked. Do not render them identically.
- **`trigger.quote`** — what someone said that caused this. Render as a quotation,
  visually distinct from Kindred's own words. **Clicking it jumps to that line in the
  transcript.**
- **`body_md`** — the reasoning. Markdown. This is the substance; give it room.
- **`citations[]`** — expandable. Each has `filename`, `page`, and `quote`. **Render
  `quote` verbatim** — it is the evidence, and paraphrasing it destroys the entire point.
  Clicking a citation opens that document in the Sources view.
- **`chat_alert`** — what the meeting actually saw in Google Meet chat. Show it, visually
  marked as "what was posted", distinct from `body_md`. Users will ask "what did everyone
  else see?" and this answers it.
- **`spoken`** — if true, Kindred said this out loud. Worth an icon; it is a meaningfully
  louder action than typing in chat.

An interjection with **no citations** is Kindred asserting something unsupported. That is
exactly what an auditor is looking for, so make it visible rather than rendering an empty
section.

Deep-linkable: `/sessions/[id]?claim=itj_…` should scroll to and highlight one card.
`api.meetings.interjection(id, interjectionId)` fetches one directly.

---

#### View B — Transcript

`bundle.transcript` — finalized segments, chronological.

- Group consecutive segments by the same speaker.
- Speaker name from `speaker_name`; colour-code consistently with the roster.
- **Mark segments that triggered an interjection.** Any segment whose id appears in some
  `interjection.trigger.segment_ids` gets an inline marker linking to that claim. This is
  the reverse direction of the core traversal and it is easy to forget.
- Segments where `person_id` is null are unidentified speakers — style them differently.
  Kindred had no context on that person, which affects how much to trust its reasoning
  about what they said.

Search, server-side:

```ts
const hits = await api.meetings.search(id, { q: "revenue", person_id: personId });
```

Both params optional. Filter-by-speaker plus text search covers essentially every "what
did they say about X" question, which is most of why someone opens a transcript.

---

#### View C — Sources

`bundle.sources` — `CitedDocument[]`, ordered by citation count.

**This is the audit surface and the reason the product is trustworthy.** It answers: *of
everything Kindred could have read, what did it actually use?*

Per document: `filename`, `citation_count`, and the `quotes[]` it pulled. Each
`SourceQuote` has `page`, `quote`, `relevance`, and `interjection_id` — so every passage
links back to the claim that used it.

Make the bidirectional traversal obvious:

```
claim ──▶ citation ──▶ source document ──▶ every other claim that used it
```

That last hop is quietly valuable: "the Q3 deck was cited in four separate claims" tells
you where the meeting's real disagreement was.

---

### 4.3 Settings — `/settings`

Four sections. Less design-sensitive than review; make it correct and legible.

**People** — CRUD over `api.people`. `display_name`, `role`, `org`, `email`, `bio`,
`aliases[]`. Worth a sentence in the UI: this is how Kindred knows who is talking and why
they would say it. `role` and `bio` are fed to the reasoning model, so "VP Finance who
owns the revenue model" genuinely changes how a revenue claim is judged. `aliases` matter
because transcripts say "Sarah" when the roster says "Sarah Chen".

**Documents** — `api.documents`. Drag-drop upload, status chip
(`pending → parsing → embedding → ready → failed`), tags, delete. Upload returns
immediately with `status: "pending"`; progress arrives on the **global socket** as
`document.status_changed`.

**Integrations** — Slack, Gmail, Drive, Notion, Salesforce. Connect resolves after ~1.2s.
**Every one is simulated.** Render a "Demo" badge driven by `is_stub`, never by a
hardcoded list — when a connection becomes real the backend flips that flag and your
badge disappears with no code change.

**Agent** — `api.settings`. The one that needs care is `autonomy`, because it is a trust
decision, not a preference. Use plain language:

- `silent` — "Kindred never speaks or posts. Findings appear here only."
- `propose` — "Kindred asks before posting to the meeting."
- `auto_post` — "Kindred posts to the meeting chat on its own." ← current default

Also here: `wake_word` (default **"Hey AGI"**), `wake_aliases` (default `["Kindred"]`),
`interjection.min_confidence` / `cooldown_seconds` / `max_per_meeting`, voice, persona.

⚠️ **PATCH replaces nested objects wholesale.** To change one cooldown, send the complete
`interjection` object. Sending `{interjection: {cooldown_seconds: 60}}` will reset the
other two fields to defaults.

---

## 5. Live mode

When `meeting.state === "in_call"`, `/sessions/[id]` becomes live. **Same route** — a
meeting does not change identity when it ends, and a separate `/live/[id]` means a URL
that breaks the moment the meeting finishes.

```ts
const conn = connectMeeting(id, {
  onSnapshot: (data) => resetStore(data),   // fires on connect AND after any gap
  onEvent: (event) => {
    switch (event.type) {
      case "transcript.partial":    upsertLiveLine(event.data); break;
      case "transcript.final":      commitSegment(event.data);  break;
      case "interjection.proposed": addClaim(event.data);       break;
      case "interjection.updated":  updateClaim(event.data);    break;
      case "agent.state_changed":   setAgentPill(event.data.agent_state); break;
      case "speech.wake_detected":  flashWakeIndicator();       break;
      case "participant.speaking_changed": setSpeaking(event.data); break;
      default: break;               // new event types arrive unannounced
    }
  },
});
// conn.close() on unmount
```

Live mode adds four things to the review layout:

1. **Agent state pill** — `idle` · `listening` · `thinking` · `speaking` · `muted`, each
   visually distinct. This pill *is* the demo. An audience watches it flip to `listening`
   the instant someone says "Hey AGI", and that moment is the product. Make the transition
   unmissable.
2. **Live roster** with speaking indicators.
3. **Live transcript line** — partials render as one dim, mutable line per speaker that
   solidifies on final. Never append partials to the log.
4. **Operator controls** — mute (`api.meetings.mute`), manual wake (`api.meetings.wake`),
   and an ask box (`api.meetings.ask`). These are stage insurance: if wake-word detection
   misfires during the demo, they are how it gets driven from the dashboard instead. Make
   them **visible controls**, not a debug panel.

Under `autonomy: "propose"`, claims arrive as `status: "proposed"` and need
approve/dismiss buttons (`api.interjections.approve(id, editedChatAlert?)`). Approve
accepts an edited `chat_alert` so an operator can tighten the wording before it posts.

---

## 6. Rules that will save you an afternoon

**Partials are not transcript entries.** `transcript.partial` fires several times per
second. Render one mutable line per speaker; append to the log only on
`transcript.final`. A partial and its final share an `id`, so the final replaces it rather
than adding a row.

**`snapshot` can fire more than once.** It means *discard local state and use this*. It
arrives on connect and again after a sequence gap the server could not replay.

**Always `default:` in your event switch.** New event types get added without
announcement. An old frontend must degrade, not crash.

**Render `citation.quote` and `trigger.quote` verbatim.** They are evidence. Truncate with
an expander if you must, never paraphrase or reflow.

**Do not surface `confidence` in the UI.** The field exists on `Interjection` and the
backend uses it to rate-limit, but it is the model's self-report and does not belong in
front of a user. The evidence is the argument; a number next to it adds nothing and
invites being read as an accuracy rate. Ignore the field.

**`chat_alert` ≤ 500 characters** because Google Meet caps chat there. That constraint is
why an interjection has both a short `chat_alert` (what the meeting saw) and a long
`body_md` + `citations` (what you render). The claim card carries the argument that chat
physically cannot fit — which is why it deserves the most design attention in the app.

**Timestamps are RFC 3339 UTC with milliseconds**, always: `2026-08-01T18:22:04.118Z`.
Render in the viewer's local timezone.

**Errors are always `{"error": {code, message, detail}}`.** `client.ts` unpacks this into
`ApiError` with `.status`, `.code`, `.detail`. One catch block shape everywhere.

**`transcript.duration_seconds` and the stats counts are derived**, recomputed server-side.
Do not compute your own totals from array lengths — a paginated response will disagree
with the header.

---

## 7. Design direction

This is a **reading tool**, not a metrics dashboard. No gauges, no sparklines, no KPI
tiles. The content is prose, quotations, and provenance.

- **Density over decoration.** A session has 20+ transcript groups and several claims with
  nested citations. Generous line-height and restrained chrome beat cards-in-cards.
- **Typographic hierarchy carries the structure.** Three distinct voices need to stay
  visually separate at a glance: what a **human said** (`trigger.quote`), what **Kindred
  concluded** (`body_md`), and what a **document says** (`citation.quote`). Give the two
  quotation types a consistent treatment — a left rule, a tinted ground, a monospace face
  — and never let them read like Kindred's own prose.
- **Provenance is the visual motif.** Every claim should look *attached* to its evidence,
  not merely adjacent to it. Connecting lines, shared accent colour per document, hover
  affordances between linked elements.
- **Colour carries meaning, sparingly.** Reserve it for `kind` badges, speaker identity,
  and the agent state pill. Everything else neutral.
- **Restraint on `contradiction`.** It is tempting to make it alarming — red, bold, an
  icon. But Kindred is contradicting a person, sometimes wrongly, and an aggressive
  treatment makes a false positive feel like an accusation. Make it *noticeable* and
  *neutral*, and let the evidence argue.

Both light and dark should work; the demo room's projector is not something you control.

---

## 8. Build order

1. **API smoke test** — one page calling `api.meetings.list()` and dumping JSON. Proves
   the backend, CORS, and types all work before any design exists.
2. **Session list** with real fixture data.
3. **Session detail — Claims view.** The core screen. Get the claim card right; it is the
   thing people screenshot.
4. **Transcript view** with claim markers and search.
5. **Sources view** with bidirectional links.
6. **Settings** — all four sections.
7. **Live mode** — socket, agent pill, operator controls.
8. Polish: deep links, empty states, keyboard nav, loading skeletons.

Steps 1–5 are the product. Ship those well before starting 7.

---

## 9. Backend state you should know

| Area | Status |
|---|---|
| Session list, detail, bundle, sources, search | ✅ real, persisted to disk |
| People / documents / integrations / settings CRUD | ✅ real, in-memory |
| Live event stream (harness) | ✅ complete |
| Interjection reasoning + citations | ✅ real via Claude when a key is set; canned fixture content otherwise — **same shapes either way** |
| Document ingestion progress | 🟡 simulated on a timer |
| Integrations | 🟡 all simulated; `is_stub: true` |
| Real Google Meet: join, speak, chat | ✅ works with `RECALL_API_KEY` |
| Real Google Meet: **hearing the meeting** | ⬜ **not built** — live transcript ingestion is the backend's open work |

That last row is worth understanding: today Kindred can join a real meeting and talk, but
its transcript only comes from the fixture harness. It does not change anything you build
— the event stream and the session records are identical either way — but it is why every
session you see during development says `source: "harness"`.

Every shape above is final. Reasoning becoming real will not change the contract.

---

## 10. Working with the backend

The contract lives in `backend/app/schemas/` (Pydantic) → `openapi.json` →
`frontend/src/lib/api/generated.ts`.

- After a backend schema change: `./scripts/gen-types.sh`, then commit both outputs.
- Adding fields, enum variants, and event types is **free and unannounced** — code
  defensively.
- Renames and removals get a heads-up first.
- Found a type that is wrong or a field you need? Ask rather than working around it in the
  frontend. Fixing it in the schema fixes it for both of us, permanently.
