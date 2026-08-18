"""App-level health: liveness plus the engine's *last-known* readiness.

Reports ``readiness.last_known()`` — a passive snapshot — and does NOT call ``ensure_ready()``: that seam
probes the engine's model list (and, once S5 fills it in, would START the GPU container), so a health
check must never trigger it or it would wake/keep the engine warm and defeat idle-down. The worker is the
sole trigger of ``ensure_ready()``. Never calls the engine's own health route either (E-15, R-16). Before
the worker has observed readiness once, ``last_known()`` is ``warming`` (INV-6, contract §S2 scope).
"""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict:
    """Return app liveness, the engine's last-known readiness, and (A-S5-03/AR-1) the sanitized cause of the
    last non-READY result so the warming state is surfaced *with its cause* to a waiting caller."""
    readiness = request.app.state.readiness
    state = readiness.last_known()
    return {"status": "ok", "engine": state.value, "engine_cause": readiness.last_cause()}
