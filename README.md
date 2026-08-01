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

🚧 Design phase. See **[DESIGN.md](./DESIGN.md)** for the full architecture and the
frontend/backend API contract.

## Quick links

- [Architecture](./DESIGN.md#3-architecture)
- [**API contract**](./DESIGN.md#8-api-contract) ← start here if you're building the frontend
- [Build order](./DESIGN.md#11-build-order)
- [Verified platform capabilities](./DESIGN.md#2-confirmed-platform-capabilities)

## Working on this

The contract lives in `backend/app/schemas/` as Pydantic models. FastAPI generates
OpenAPI from them, and `scripts/gen-types.sh` generates
`frontend/src/lib/api/generated.ts`. **Never hand-write an API type in the frontend.**

A fixture-replay dev harness (`POST /api/dev/harness/start`) emits the complete real-time
event stream from a scripted transcript, so the frontend can be built end to end without
a live meeting, a Recall API key, or an internet connection.

```bash
docker compose up -d      # postgres + pgvector
make dev                  # backend on :8000, frontend on :3000
```
