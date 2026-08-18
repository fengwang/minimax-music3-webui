"""Strategy-B stub backend for the browser harness (docs/session_2/branch_compare.md).

A minimal, GPU-free FastAPI app that serves the REAL api/app/static/ directory through the SAME
StaticFiles(html=True) catch-all production mounts LAST (api/app/main.py:146), with test-controlled stub
routes registered BEFORE it so route precedence matches production. The stub surface is a strict SUBSET of
ledger §4: the five SSE event names, failed -> {error}, the heartbeat as an SSE comment, /health engine in
{ready, warming, unavailable}, and the exact /artifacts + sidecar field sets. It invents no progress
percentage, ETA, queue depth, or any field the frozen backend does not have.

Extension seam for S3/S4/S5: import create_stub_app, drive behaviour through app.state.stub (a StubState,
reset per test by tests/ui/conftest.py's `stub` fixture), and add tests under tests/ui/. Scripted SSE
sequences SHOULD end in a terminal event (succeeded/failed/cancelled), matching the real stream close; a
non-terminal sequence emits a single heartbeat comment and then closes (used by the fidelity test only).
"""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# The real static directory — served, never copied or edited: <repo>/api/app/static.
_STATIC_DIR = Path(__file__).resolve().parents[2] / "api" / "app" / "static"

#: The five lifecycle event names the client registers (app.js:147). The stub emits only these.
SSE_EVENT_NAMES = ("queued", "running", "succeeded", "failed", "cancelled")
#: Events that end a stream (mirror jobs.events.TERMINAL_EVENT_TYPES).
TERMINAL_EVENT_TYPES = frozenset({"succeeded", "failed", "cancelled"})
#: The only engine states /health reports (api/app/health.py; contract §4).
ENGINE_STATES = ("ready", "warming", "unavailable")

# A tiny but structurally valid 44-byte WAV (header, zero samples): enough for an <audio> src to resolve
# without a GPU or a real generation. 32000 Hz / 2 ch / 16-bit — the engine's only output format.
_WAV_STUB = (
    b"RIFF" + (36).to_bytes(4, "little") + b"WAVE"
    + b"fmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (2).to_bytes(2, "little")
    + (32000).to_bytes(4, "little") + (128000).to_bytes(4, "little")
    + (4).to_bytes(2, "little") + (16).to_bytes(2, "little")
    + b"data" + (0).to_bytes(4, "little")
)


def make_pcm_wav(seconds: float = 1.0, rate: int = 32000, channels: int = 2, freq: float = 220.0) -> bytes:
    """A canonical 16-bit PCM WAV with real samples, so ``audio.duration`` is non-zero and the hand-rolled
    peak reader (spec: lazy-waveform) has something to parse. A fading sine (amplitude 1.0 -> 0.2 across the
    file) makes the extracted peaks non-trivial. ``rate`` is parameterizable so a test can prove the time
    axis is not hardcoded to 32000. Kept small (<= a few hundred KB at test durations)."""
    frames = int(seconds * rate)
    body = bytearray()
    for i in range(frames):
        env = 1.0 - (i / frames) * 0.8 if frames else 0.0
        sample = int(max(-1.0, min(1.0, env * math.sin(2 * math.pi * freq * i / rate))) * 32767)
        body += struct.pack("<" + "h" * channels, *([sample] * channels))
    n = len(body)
    header = (
        b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * 2, channels * 2, 16)
        + b"data" + struct.pack("<I", n)
    )
    return header + bytes(body)


def make_sidecar(
    job_id: str = "job-demo", *, seed: int = 0, max_new_tokens: int = 7500,
    duration_seconds: float = 279.29,
) -> dict:
    """A §4-faithful sidecar. duration_seconds < max_new_tokens/25 to honour cap-not-target (P2-E03)."""
    return {
        "sidecar_version": "1",
        "job_id": job_id,
        "trace_id": job_id,
        "engine": {"name": "sglang", "version": "stub", "image_digest": "sha256:stub"},
        "model": {"id": "MiniMaxAI/MiniMax-Music3", "path": "/models/stub"},
        "request": {
            "input": "[Verse]\nstub lyric",
            "instructions": "Global Metadata: stub",
            "seed": seed,
            "max_new_tokens": max_new_tokens,
            "translated": None,
        },
        "timings": {
            "submitted_at": "2026-01-01T00:00:00+00:00",
            "started_at": "2026-01-01T00:00:01+00:00",
            "ended_at": "2026-01-01T00:05:00+00:00",
            "generation_seconds": duration_seconds,
            "queue_seconds": 1.0,
            "total_seconds": duration_seconds + 1.0,
        },
        "output": {
            "sample_rate": 32000,
            "channels": 2,
            "duration_seconds": duration_seconds,
            "byte_size": int(duration_seconds * 32000 * 2 * 2),
            "content_hash": "sha256:stub",
            "content_type": "audio/wav",
        },
    }


def make_listing_item(job_id: str = "job-demo", **sidecar_kwargs) -> dict:
    """A §4-faithful /artifacts listing item, spliced from the matching sidecar (as scan_listing does)."""
    sidecar = make_sidecar(job_id, **sidecar_kwargs)
    return {
        "job_id": job_id,
        "audio_url": f"/artifacts/{job_id}/audio.wav",
        "sidecar_url": f"/artifacts/{job_id}/sidecar.json",
        "mtime": 1_700_000_000.0,
        "engine": sidecar["engine"],
        "model": sidecar["model"],
        "output": sidecar["output"],
        "timings": sidecar["timings"],
    }


@dataclass
class StubState:
    """Test-controlled backend state, reset per test by the `stub` fixture."""

    engine: str = "ready"
    engine_cause: str = ""
    events: list[tuple[str, dict]] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    sidecars: dict[str, dict] = field(default_factory=dict)
    #: Optional per-job audio bodies. When a job_id is present, /artifacts/{id}/audio.wav serves it
    #: (a real-sample make_pcm_wav for waveform/seek tests); otherwise the zero-sample _WAV_STUB is used.
    audio_bodies: dict[str, bytes] = field(default_factory=dict)
    #: job_ids whose /artifacts/{id}/audio.wav returns 404 (to exercise the player's error path).
    audio_missing: set[str] = field(default_factory=set)


def _render_sse(event_id: int, name: str, data: dict) -> str:
    """Render one SSE frame exactly as jobs.events.render_sse does."""
    return f"id: {event_id}\nevent: {name}\ndata: {json.dumps(data)}\n\n"


def create_stub_app() -> FastAPI:
    """Build the stub app: API routes first, the production-faithful static catch-all LAST."""
    app = FastAPI()
    app.state.stub = StubState()

    @app.get("/health")
    async def health(request: Request) -> dict:
        stub: StubState = request.app.state.stub
        if stub.engine not in ENGINE_STATES:
            raise ValueError(f"stub engine state not in §4 set: {stub.engine!r}")
        return {"status": "ok", "engine": stub.engine, "engine_cause": stub.engine_cause}

    @app.post("/jobs", status_code=202)
    async def submit_job(request: Request) -> dict:
        body = await request.json()
        return {
            "id": "job-demo",
            "status": "queued",
            "params": {k: body.get(k) for k in ("input", "instructions", "seed", "max_new_tokens")},
            "submitted_at": "2026-01-01T00:00:00+00:00",
            "started_at": None,
            "ended_at": None,
            "engine": None,
            "model": None,
            "error": None,
        }

    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request) -> StreamingResponse:
        sequence = list(request.app.state.stub.events)

        async def generate():
            for event_id, (name, data) in enumerate(sequence, start=1):
                if name not in SSE_EVENT_NAMES:
                    raise ValueError(f"stub SSE name not in §4 set: {name!r}")
                payload = {"error": data.get("error", "generation failed")} if name == "failed" else {}
                yield _render_sse(event_id, name, payload)
                if name in TERMINAL_EVENT_TYPES:
                    return
            # No terminal event: emit the heartbeat as an SSE COMMENT (EventSource fires no callback,
            # jobs.py:153 yields exactly this), then close.
            yield ": heartbeat\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/artifacts")
    async def list_artifacts(request: Request) -> list[dict]:
        return request.app.state.stub.artifacts

    @app.get("/artifacts/{job_id}/{name}")
    async def get_artifact(job_id: str, name: str, request: Request):
        if name == "sidecar.json":
            sidecar = request.app.state.stub.sidecars.get(job_id)
            if sidecar is None:
                return JSONResponse({"detail": "artifact not found"}, status_code=404)
            return JSONResponse(sidecar)
        if name == "audio.wav":
            stub = request.app.state.stub
            if job_id in stub.audio_missing:
                return JSONResponse({"detail": "artifact not found"}, status_code=404)
            body = stub.audio_bodies.get(job_id, _WAV_STUB)
            return Response(content=body, media_type="audio/wav")
        return JSONResponse({"detail": "artifact not found"}, status_code=404)

    # Mounted LAST, exactly as production does (api/app/main.py:146): the API routes above match first,
    # and this catch-all serves "/" -> index.html and the CSS/JS assets from the app's own origin.
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")
    return app
