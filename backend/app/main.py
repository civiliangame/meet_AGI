"""FastAPI application entrypoint.

    uvicorn app.main:app --reload --port 8000

Runs with an entirely empty environment. No database, no Recall key, no Anthropic key.
Missing credentials degrade specific features and are reported in `GET /api/health`;
nothing crashes on import. That is deliberate — the frontend must be buildable on a
laptop with no secrets.

Two things worth knowing if you are reading this to understand the contract:

- OpenAPI lives at `/openapi.json`, Swagger UI at `/docs`. `scripts/gen-types.sh`
  turns that document into `frontend/src/lib/api/generated.ts`.
- WebSocket event types reach the generated client via the schema-anchor endpoints in
  `app/api/schema_only.py`. OpenAPI cannot describe sockets, so those unused endpoints
  are what keep `LiveEvent` from having to be hand-written on the frontend.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import archive
from .api import api_router
from .config import get_config
from .errors import ApiError
from .ingest import recall_live
from .knowledge import get_knowledge_base
from .runtime import get_runtime, shutdown_runtime
from .video import attach_to_bus
from .schemas import ErrorResponse, Schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
"""Dev frontend origins. Next.js on 3000, Vite on 5173.

Lives here rather than in `config.py` to keep the two workstreams out of each other's
files. Move it into config when the two lanes merge.
"""

DESCRIPTION = """
Backend for **Kindred**, a Google Meet copilot that actually speaks.

### Building the frontend against this

You do not need a Google Meet, a Recall.ai key, or an internet connection. Start a
fixture replay and subscribe to the socket:

```bash
curl -X POST localhost:8000/api/dev/harness/start \\
  -H 'content-type: application/json' \\
  -d '{"fixture_id":"q3_revenue_review","speed":6,"loop":true}'
```

That emits the complete real-time event stream — participants, partial and final
transcript, speaking states, wake-word detection, agent state transitions, and
interjections with real citations. It is byte-for-byte the same contract a real meeting
produces; only `Meeting.source` differs.

### Contract rules

- Types are generated from this OpenAPI document. Never hand-write an API type.
- All mutations are REST. The WebSocket is server-to-client only.
- Adding fields, enum variants, and event types is free and unannounced. Renames and
  removals get a heads-up first.
- `switch` on `LiveEvent.type` with a `default` branch. New event types will arrive.
"""


class HealthProvider(Schema):
    """One capability and whether its credentials are present."""

    name: str
    configured: bool
    detail: str


class Health(Schema):
    status: str
    app: str
    providers: list[HealthProvider]


app = FastAPI(
    title="meet_AGI — Kindred",
    description=DESCRIPTION,
    version="0.1.0",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Declaring the error envelope here does two jobs: it documents the real error shape on
# every endpoint in Swagger, and it pulls `ErrorResponse` into `components.schemas` so the
# generated TypeScript has a type for it. A model no route references never reaches
# OpenAPI at all.
app.include_router(
    api_router,
    responses={
        400: {"model": ErrorResponse, "description": "Bad request"},
        404: {"model": ErrorResponse, "description": "Not found"},
        409: {"model": ErrorResponse, "description": "Conflict"},
        422: {"model": ErrorResponse, "description": "Validation error"},
    },
)


# --- Error handlers ---------------------------------------------------------------
# FastAPI's default error body is `{"detail": ...}`, which is not the shape the contract
# documents. These rewrite every error into `{"error": {code, message, detail}}` so the
# frontend has exactly one error parser.


@app.exception_handler(ApiError)
async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "detail": exc.extra}},
    )


@app.exception_handler(HTTPException)
async def handle_http_exception(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "http_error",
                "message": str(exc.detail),
                "detail": None,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request body or parameters failed validation.",
                "detail": {"errors": exc.errors()},
            }
        },
    )


# --- Lifecycle -------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    config = get_config()
    log.info("meet_AGI backend starting")

    # Restore past sessions before serving. The dashboard's job is reviewing finished
    # meetings, so an empty session list after a restart is a broken product, not a
    # cold cache.
    restored = archive.load_all()
    archive.start_autosave()
    log.info("restored %d archived session(s)", restored)

    # Turn the configured transcript windows into live ingest state. Skipping this is
    # how the ceiling that stops an open mic from swallowing every utterance ends up
    # never being applied.
    recall_live.configure(
        config.transcript_silence_ms,
        wake_silence_ms=config.transcript_wake_silence_ms,
        max_utterance_ms=config.transcript_max_utterance_ms,
        wake_max_ms=config.transcript_wake_max_ms,
    )

    if not config.recall_api_key:
        log.warning(
            "RECALL_API_KEY unset — real meetings unavailable. "
            "Use POST /api/dev/harness/start for a fixture-backed meeting."
        )

    # Render the "let me look that up" lines now, in the background. They are cached on
    # disk, so this is a no-op on every run after the first — but paying for synthesis
    # during the first wake of a live meeting would add latency to the one moment the
    # filler exists to cover. Never blocks startup; the fallback is a sample clip.
    async def _warm_fillers() -> None:
        try:
            count = await get_runtime().fillers.warm()
            if count:
                log.info("filler speech ready (%d lines)", count)
        except Exception:
            log.exception("could not pre-render filler speech; falling back to sample clips")

    asyncio.create_task(_warm_fillers(), name="warm-fillers")

    # Mirror agent state onto the bot's camera tile for the rest of the process.
    attach_to_bus()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    # Flush before tearing down the runtime; a session lost on shutdown is a session
    # nobody can review.
    await archive.stop_autosave()
    await shutdown_runtime()
    log.info("meet_AGI backend stopped")


# --- Health ----------------------------------------------------------------------


def _reasoning_detail(config) -> str:
    """One line naming the active reasoning backend and its models."""
    provider = config.resolved_llm_provider
    if provider == "gemini":
        return f"Gemini ({config.gemini_model}); triage on {config.gemini_fast_model}"
    if provider == "claude":
        return f"Claude ({config.anthropic_model}); triage on {config.anthropic_fast_model}"
    if provider == "tenstorrent":
        return (
            f"Tenstorrent ({config.tenstorrent_model}); triage on "
            f"{config.tenstorrent_fast_model}"
        )
    return (
        "no reasoning key set (GEMINI_API_KEY, ANTHROPIC_API_KEY or "
        "TENSTORRENT_API_KEY) — the harness replays its canned interjections instead"
    )


@app.get("/api/health", response_model=Health, tags=["health"], summary="Health check")
def health() -> Health:
    """Which capabilities are live.

    The frontend can use this to disable or annotate features whose credentials are
    missing, instead of letting the user click something that cannot work.
    """
    config = get_config()
    knowledge = get_knowledge_base()

    # A health check that 500s tells you nothing. If the runtime cannot be built at all
    # — a misconfigured provider, a missing asset — report that as degraded rather than
    # raising, so the frontend still gets an answer it can render.
    try:
        runtime = get_runtime()
    except Exception as exc:  # noqa: BLE001 — deliberately broad; this must not raise
        log.exception("runtime construction failed")
        return Health(
            status="degraded",
            app="meet_AGI",
            providers=[
                HealthProvider(
                    name="runtime",
                    configured=False,
                    detail=f"{type(exc).__name__}: {exc}",
                ),
                HealthProvider(
                    name="harness",
                    configured=True,
                    detail="fixture replay does not depend on the runtime",
                ),
            ],
        )

    return Health(
        status="ok",
        app="meet_AGI",
        providers=[
            HealthProvider(
                name="recall",
                configured=runtime.recall_configured,
                detail=(
                    f"Recall.ai bots ({config.recall_region})"
                    if runtime.recall_configured
                    else "RECALL_API_KEY unset — use the fixture harness instead"
                ),
            ),
            HealthProvider(
                name="voice",
                # `auto` is the default, so reporting it back tells you nothing. Report
                # what it actually resolved to — the point of this endpoint on stage is
                # to answer "is Kindred about to speak for real or play a canned clip?"
                configured=runtime.voice.name != "sample",
                detail=(
                    f"Inworld TTS ({config.inworld_voice_id}, {config.inworld_model_id})"
                    if runtime.voice.name == "inworld"
                    else "INWORLD_API_KEY unset — Kindred will play pre-baked sample clips"
                ),
            ),
            HealthProvider(
                name="reasoning",
                configured=config.resolved_llm_provider != "none",
                detail=_reasoning_detail(config),
            ),
            HealthProvider(
                name="knowledge",
                configured=knowledge.chunk_count > 0,
                detail=(
                    f"{knowledge.chunk_count} chunks across {knowledge.document_count} "
                    f"documents: {', '.join(knowledge.filenames)}"
                    if knowledge.chunk_count
                    else "no .txt files found in knowledge/ — nothing to check claims against"
                ),
            ),
            HealthProvider(
                name="harness",
                configured=True,
                detail="fixture replay always available",
            ),
        ],
    )


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {
        "app": "meet_AGI — Kindred",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/api/health",
    }
