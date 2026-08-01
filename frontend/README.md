# Kindred frontend

**Start here.** This directory is the frontend's contract layer, not a scaffolded app.
The app framework choice is yours — scaffold Next.js (or anything else) around what's
here.

```bash
cd frontend
npm install
npm run typecheck        # proves the generated types compile
```

To scaffold Next.js in place without clobbering `src/lib/api/`:

```bash
npx create-next-app@latest . --ts --tailwind --app --src-dir --no-git
# keep src/lib/api/ when it asks about conflicts
```

---

## Run the backend

You need **no** database, no Recall API key, no Google account, and no internet
connection.

```bash
cd backend
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e .        # Windows
# .venv/bin/pip install -e .                          # macOS / Linux
./.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

- API: http://localhost:8000
- **Swagger UI: http://localhost:8000/docs** ← browse the whole contract
- Health: http://localhost:8000/api/health

## Get a live meeting to build against

There is a fixture replay that emits the exact event stream a real Google Meet produces.
Start one:

```bash
curl -X POST localhost:8000/api/dev/harness/start \
  -H 'content-type: application/json' \
  -d '{"fixture_id":"q3_revenue_review","speed":6,"loop":true}'
```

That returns a `Meeting`. Take its `id` and connect to
`ws://localhost:8000/api/meetings/{id}/live`.

`speed: 6` with `loop: true` gives you a continuously replaying meeting — good for
building. Drop to `speed: 1` to see it at real pace.

The fixture (`fixtures/meetings/q3_revenue_review.jsonl`) is a four-person quarterly
review deliberately built to exercise every UI state:

| What happens | Which UI state it exercises |
|---|---|
| 4 participants join, one is a dial-in guest | Roster, and the **unmatched participant** flag |
| ~22 utterances with partials before each final | The **live line** that solidifies on final |
| Marcus overstates new-product revenue | An `interjection.proposed` of kind `contradiction`, with real citations |
| Priya says "Kindred, ..." twice | Full speech mode: `wake_detected` → `listening` → `thinking` → `speaking` → `answered` |
| Meeting ends | `meeting.state_changed` to `ended` |

Reset between runs with `POST /api/dev/reset`.

---

## Using the API layer

```ts
import { api, ApiError } from "@/lib/api/client";
import { connectMeeting } from "@/lib/api/ws";
import type { Interjection, LiveEvent } from "@/lib/api/types";

const people = await api.people.list();
const meeting = await api.dev.startHarness("q3_revenue_review", 6, true);

const conn = connectMeeting(meeting.id, {
  onSnapshot: (data) => resetStore(data),        // full state; also fires after a gap
  onEvent: (event) => {
    switch (event.type) {
      case "transcript.partial":     upsertLiveLine(event.data); break;
      case "transcript.final":       commitSegment(event.data);  break;
      case "interjection.proposed":  addCard(event.data);        break;
      case "agent.state_changed":    setAgentPill(event.data.agent_state); break;
      case "speech.wake_detected":   flashWakeIndicator();       break;
      default: break;                // new event types arrive unannounced
    }
  },
  onStatus: (s) => setConnectionBadge(s),
});
// conn.close() on unmount
```

### Files

| File | What it is |
|---|---|
| `src/lib/api/generated.ts` | **Generated. Never edit.** Output of `./scripts/gen-types.sh`. |
| `src/lib/api/types.ts` | Readable aliases over the generated types. Import from here. |
| `src/lib/api/client.ts` | Typed REST client. Throws `ApiError` on non-2xx. |
| `src/lib/api/ws.ts` | Typed socket client: reconnect, `since_seq` replay, gap detection. |

Point at a non-default backend with `NEXT_PUBLIC_API_BASE`.

---

## Rules that will save you a bad afternoon

**Never hand-write an API type.** Everything comes from `backend/app/schemas/` via
OpenAPI. If a type looks wrong, the Pydantic model is wrong — say so and it gets fixed at
the source. Hand-edits to `generated.ts` are destroyed on the next codegen run.

**Always put a `default` in your `switch` on `event.type`.** New event types get added
without announcement. An older frontend must degrade, not crash.

**Partials are not transcript entries.** `transcript.partial` frames arrive several times
per second. Render them as one mutable line per speaker; only append to the transcript log
on `transcript.final`. Partials and their final share an `id`, so a final replaces its
partial rather than adding a row.

**`snapshot` can fire more than once.** It means "discard local state and use this". It
arrives on connect, and again after a sequence gap the server could not replay.

**Chat alerts are capped at 500 characters** because Google Meet caps them there. That is
why `Interjection` has both a short `chat_alert` (what the meeting saw) and a long
`body_md` + `citations` (what you render). The card is the payoff — it carries the
argument chat cannot fit. Give it the most design attention.

**`is_stub: true` on an integration means it is simulated.** Render a "Demo" badge off
that flag rather than hardcoding which providers are fake; when a connection becomes real
the flag flips and your badge disappears with no code change.

---

## What is real vs. stubbed right now

| Area | State |
|---|---|
| People, documents, integrations, settings CRUD | **Real**, in-memory, seeded with demo data |
| Fixture harness event stream | **Real** and complete |
| Document ingestion progression | **Simulated** on a timer (`pending → parsing → embedding → ready`) |
| Interjection reasoning + citations | **Canned** fixture content. Shapes are final; Milestone 4 makes reasoning real |
| `POST /api/meetings` (real Recall bot) | Works when `RECALL_API_KEY` is set; returns a `failed` meeting with an explanatory `error` when it is not |
| Persistence | **None.** Restarting the backend resets everything |

Every shape above is final. Reasoning becoming real will not change the contract.
