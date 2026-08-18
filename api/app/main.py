"""FastAPI app composition, MUSIC3_ configuration, INV-13 startup log, and the single-worker lifespan.

The imperative shell / composition root: it reads the environment once (an Action), builds the immutable
``Config`` (a pure parse), constructs the store, the single engine client, the single-slot runner, and the
readiness seam, injects them onto ``app.state``, and wires the routes. ``config_from_env`` and
``startup_banner`` are pure Calculations. Dependencies are injectable so tests need no GPU, weights, or
reachable engine.
"""

import asyncio
import logging
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.health import router as health_router
from app.routes.artifacts import router as artifacts_router
from app.routes.jobs import router as jobs_router
from app.routes.music import MAX_CONCURRENT_DELIVERIES  # A-S4-01
from app.routes.music import router as music_router  # A-S4-01: the cloud-compatible edge
from engines.sglang.client import SglangClient
from jobs.artifacts import validate_artifacts_root
from jobs.runner import JobRunner
from jobs.store import JobStore
from lifecycle.readiness import Readiness, make_models_probe

_log = logging.getLogger("minimax_music3")

# Default bind is all interfaces: the service is LAN-exposed by owner decision D5 and logged as
# unauthenticated at startup (INV-13); narrow it to loopback via MUSIC3_BIND_ADDR (accepted risk R-09).
_DEFAULT_BIND_ADDR = "0.0.0.0"


@dataclass(frozen=True)
class Config:
    """Immutable configuration snapshot, parsed once from the environment at startup."""

    bind_addr: str
    app_port: int
    engine_base_url: str
    engine_model: str
    engine_read_timeout_seconds: float
    readiness_probe_timeout_seconds: float
    readiness_cache_seconds: float
    sse_heartbeat_seconds: float
    event_log_limit: int
    # S3: the app's artifacts root. None when MUSIC3_ARTIFACTS_DIR is unset → persistence is off and no
    # startup validation runs (keeps the CPU test suite building the app from an empty environment).
    artifacts_dir: str | None


def config_from_env(env: Mapping[str, str]) -> Config:
    """Parse a MUSIC3_-prefixed environment snapshot into a Config. Pure (no ``os.environ`` read here).

    Raises ``ValueError`` on an out-of-range value (e.g. a zero event-log limit that would drop terminal
    SSE events) so a bad host configuration fails loudly at startup rather than corrupting behaviour.
    """
    event_log_limit = int(env.get("MUSIC3_EVENT_LOG_LIMIT", "1024"))
    if event_log_limit < 1:
        raise ValueError(f"MUSIC3_EVENT_LOG_LIMIT must be >= 1; got {event_log_limit}")
    return Config(
        bind_addr=env.get("MUSIC3_BIND_ADDR", _DEFAULT_BIND_ADDR),
        app_port=int(env.get("MUSIC3_APP_PORT", "8080")),
        engine_base_url=env.get("MUSIC3_ENGINE_BASE_URL", "http://sglang:8000"),
        engine_model=env.get("MUSIC3_ENGINE_MODEL", "MiniMaxAI/MiniMax-Music3"),
        engine_read_timeout_seconds=float(env.get("MUSIC3_ENGINE_READ_TIMEOUT_SECONDS", "1200")),
        readiness_probe_timeout_seconds=float(env.get("MUSIC3_READINESS_PROBE_TIMEOUT_SECONDS", "10")),
        readiness_cache_seconds=float(env.get("MUSIC3_READINESS_CACHE_SECONDS", "5")),
        sse_heartbeat_seconds=float(env.get("MUSIC3_SSE_HEARTBEAT_SECONDS", "15")),
        event_log_limit=event_log_limit,
        artifacts_dir=env.get("MUSIC3_ARTIFACTS_DIR") or None,
    )


def startup_banner(config: Config) -> str:
    """The INV-13 startup line: states the bind address and that the service is unauthenticated. Pure."""
    return (
        f"MiniMax-Music3 API listening on {config.bind_addr}:{config.app_port} — "
        "UNAUTHENTICATED (LAN-exposed by owner decision D5; accepted risk R-09)"
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def create_app(*, config: Config | None = None, engine=None, readiness: Readiness | None = None) -> FastAPI:
    """Compose the app. Reading ``os.environ`` is the only Action; downstream receives explicit objects."""
    config = config or config_from_env(os.environ)
    # S3 fail-fast: when an artifacts root is configured it must exist, be a directory, and be writable,
    # or startup aborts naming the resolved path (INV-8 storage precondition; deterministic check 6).
    if config.artifacts_dir is not None:
        validate_artifacts_root(config.artifacts_dir)
    engine = engine or SglangClient(
        base_url=config.engine_base_url,
        model=config.engine_model,
        read_timeout=config.engine_read_timeout_seconds,
    )
    readiness = readiness or Readiness(
        make_models_probe(config.engine_base_url, config.readiness_probe_timeout_seconds),
        cache_seconds=config.readiness_cache_seconds,
    )
    store = JobStore(event_log_limit=config.event_log_limit)
    runner = JobRunner(
        store,
        engine,
        model=config.engine_model,
        clock=_utc_now,
        readiness=readiness,
        artifacts_root=config.artifacts_dir,  # S3: None → no persistence (S2 behaviour preserved)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _log.warning(startup_banner(app.state.config))  # INV-13
        app.state.runner.start()
        # A-S5-02 (codex CR-1): arm idle-down at boot, not lazily on the first job — `docker compose up`
        # starts the GPU container immediately, so an initially-idle deployment must still free VRAM.
        app.state.readiness.start_idle_monitor()
        try:
            yield
        finally:
            await app.state.runner.stop()
            await app.state.readiness.aclose()  # A-S5-02: tear the idle monitor down cleanly on shutdown

    app = FastAPI(title="MiniMax-Music3 API", lifespan=lifespan)
    app.state.config = config
    app.state.store = store
    app.state.runner = runner
    app.state.readiness = readiness
    # S4 (A-S4-01): bound concurrent delivery transcodes so a burst of url GETs cannot spawn N ffmpegs.
    app.state.delivery_slots = asyncio.Semaphore(MAX_CONCURRENT_DELIVERIES)
    app.include_router(health_router)
    app.include_router(jobs_router)
    app.include_router(artifacts_router)  # S3: always mounted so the OpenAPI schema is env-independent
    app.include_router(music_router)  # S4 (A-S4-01): cloud POST /v1/music_generation + url delivery route
    # S6: serve the hand-written static WebUI. Mounted LAST so the API routers above (and FastAPI's own
    # /openapi.json) match first; this catch-all only serves "/" -> index.html and the CSS/JS assets, and
    # is the ONLY edit S6 makes to api/app/main.py (contract: static mount only, route surface stays S2's).
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
    return app
