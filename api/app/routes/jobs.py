"""The native job API: submit, status, SSE progress, cancel.

Native passthrough only — the body carries the engine's native fields (``input``, ``instructions``,
``seed``, ``max_new_tokens``), never a cloud envelope (that translation is S4). The frame ceiling is
enforced here at the boundary via a Pydantic constraint, so an over-ceiling request is refused with HTTP
422 before it ever reaches the engine (E-08, E-18).

Dependencies are read from ``request.app.state`` (the runner, store, and config the lifespan wires up),
deliberately avoiding FastAPI's ``Depends()/Query()`` default-argument style. The SSE generator holds no
per-subscriber registration and decrements a live-stream counter in a ``finally``, so a client disconnect
can never leak a consumer (adversarial case 6).
"""

import asyncio
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from engines.sglang.client import MAX_NEW_TOKENS_CEILING
from jobs.events import TERMINAL_EVENT_TYPES, events_since, render_sse
from jobs.store import JobNotFound, JobRecord, JobTransitionError, Submission

router = APIRouter()

_POLL_SECONDS = 0.05
# A one-slot mutable counter of live SSE generators; a client disconnect must return it to zero.
_active_streams = {"count": 0}


def active_stream_count() -> int:
    """Number of SSE generators currently live (0 when idle). Used to prove no consumer leak."""
    return _active_streams["count"]


class SubmitRequest(BaseModel):
    """The native submission body. ``max_new_tokens`` is capped at the documented ceiling (E-08, E-18).

    ``extra="forbid"`` rejects any non-native field (a cloud field such as ``model``/``prompt``/
    ``audio_setting``, or ``stream``) with HTTP 422 rather than silently discarding it — cloud-schema
    translation is S4's, and S2 must not let an unsupported field appear honoured (INV-7 boundary).
    """

    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    seed: int = Field(default=0, ge=0)
    max_new_tokens: int = Field(ge=1, le=MAX_NEW_TOKENS_CEILING)


def _job_view(record: JobRecord) -> dict:
    """Render a job record as the public status view — pure. Never includes the audio bytes."""
    return {
        "id": record.id,
        "status": record.status.value,
        "params": {
            "input": record.submission.input,
            "instructions": record.submission.instructions,
            "seed": record.submission.seed,
            "max_new_tokens": record.submission.max_new_tokens,
        },
        "submitted_at": record.submitted_at,
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "engine": record.engine,
        "model": record.model,
        "error": record.error,
    }


def _parse_last_event_id(raw: str | None) -> int | None:
    return int(raw) if raw is not None and raw.isdigit() else None


@router.post("/jobs", status_code=202)
async def submit_job(payload: SubmitRequest, request: Request) -> dict:
    """Accept a native submission, queue it for the single worker, and return the queued job (202)."""
    submission = Submission(
        input=payload.input,
        instructions=payload.instructions,
        seed=payload.seed,
        max_new_tokens=payload.max_new_tokens,
    )
    record = await request.app.state.runner.submit(submission)
    return _job_view(record)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> dict:
    """Return the job's current status view, or 404 if the id is unknown."""
    try:
        return _job_view(request.app.state.store.get(job_id))
    except JobNotFound:
        raise HTTPException(status_code=404, detail="job not found") from None


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> dict:
    """Cancel a queued or running job (200); 404 if unknown, 409 if already terminal."""
    try:
        return _job_view(await request.app.state.runner.cancel(job_id))
    except JobNotFound:
        raise HTTPException(status_code=404, detail="job not found") from None
    except JobTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/jobs/{job_id}/events")
async def stream_events(job_id: str, request: Request) -> StreamingResponse:
    """Stream the job's lifecycle events as SSE, honoring ``Last-Event-ID``; 404 if the id is unknown."""
    store = request.app.state.store
    try:
        store.get(job_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail="job not found") from None
    last_id = _parse_last_event_id(request.headers.get("last-event-id"))
    heartbeat_seconds = request.app.state.config.sse_heartbeat_seconds
    return StreamingResponse(
        _event_stream(request, store, job_id, last_id, heartbeat_seconds),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _event_stream(
    request: Request,
    store,
    job_id: str,
    last_id: int | None,
    heartbeat_seconds: float,
) -> AsyncIterator[str]:
    """Replay retained events after ``last_id``, then tail live ones, ending on the terminal event.

    Leak-proof: no per-subscriber registration; the ``finally`` always runs (on terminal, on client
    disconnect, or on task cancellation), returning the live-stream counter to its prior value.
    """
    _active_streams["count"] += 1
    last_beat = time.monotonic()
    try:
        while True:
            for event in events_since(store.log(job_id), last_id):
                last_id = event.id
                yield render_sse(event)
                if event.type in TERMINAL_EVENT_TYPES:
                    return
            if await request.is_disconnected():
                return
            now = time.monotonic()
            if now - last_beat >= heartbeat_seconds:
                yield ": heartbeat\n\n"
                last_beat = now
            await asyncio.sleep(_POLL_SECONDS)
    finally:
        _active_streams["count"] -= 1
