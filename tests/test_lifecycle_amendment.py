"""Amendment A-S5-01: the runner holds the readiness lease across the whole engine call, and only then
(spec: idle-down-lease.md). CPU-only, fake engine + FakeReadiness that counts leases.

The lease is what lets idle-down stay correct without a wall-clock guess: while a generation is in flight
the lease is held, so idle-down cannot stop the container under it (adversarial case 3).
"""

import asyncio
from types import SimpleNamespace

from app.health import health
from fakes import FakeEngineClient, FakeReadiness
from jobs.runner import JobRunner
from jobs.store import JobStatus, JobStore, Submission
from lifecycle.readiness import ReadinessState

_SUB = Submission(input="[Verse]\nhi", instructions="warm", seed=0, max_new_tokens=250)


def _runner(engine: FakeEngineClient, readiness: FakeReadiness) -> tuple[JobRunner, JobStore]:
    store = JobStore()
    runner = JobRunner(store, engine, model="MiniMaxAI/MiniMax-Music3", clock=lambda: "t", readiness=readiness)
    return runner, store


async def _wait_status(store: JobStore, job_id: str, status: JobStatus, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if store.get(job_id).status is status:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"{job_id} never reached {status}; is {store.get(job_id).status}")


async def _wait_terminal(store: JobStore, job_id: str, timeout: float = 1.0) -> None:
    terminal = {JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled}
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if store.get(job_id).status in terminal:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"{job_id} never reached a terminal state")


async def test_lease_held_while_generating_then_released() -> None:
    engine = FakeEngineClient(default="hang")
    readiness = FakeReadiness(ReadinessState.READY)
    runner, store = _runner(engine, readiness)
    runner.start()
    try:
        a = await runner.submit(_SUB)
        await _wait_status(store, a.id, JobStatus.running)
        assert readiness.leases == 1  # lease held for the duration of the engine call
        engine.release()
        await _wait_terminal(store, a.id)
        assert readiness.leases == 0  # released after completion
        assert readiness.max_leases == 1
    finally:
        await runner.stop()


async def test_lease_released_on_engine_failure() -> None:
    engine = FakeEngineClient(default="raise")
    readiness = FakeReadiness(ReadinessState.READY)
    runner, store = _runner(engine, readiness)
    runner.start()
    try:
        a = await runner.submit(_SUB)
        await _wait_terminal(store, a.id)
        assert store.get(a.id).status is JobStatus.failed
        assert readiness.leases == 0  # released despite the engine raising
        assert readiness.max_leases == 1
    finally:
        await runner.stop()


async def test_lease_released_on_cancel() -> None:
    engine = FakeEngineClient(default="hang")
    readiness = FakeReadiness(ReadinessState.READY)
    runner, store = _runner(engine, readiness)
    runner.start()
    try:
        a = await runner.submit(_SUB)
        await _wait_status(store, a.id, JobStatus.running)
        assert readiness.leases == 1
        await runner.cancel(a.id)
        # Yield so the worker unwinds the aborted engine call before we assert/stop — cancel() sets the
        # status synchronously, so waiting on status alone would not let the lease release (and would call
        # stop() mid-_process, hitting an unrelated pre-existing runner teardown race).
        for _ in range(100):
            if readiness.leases == 0:
                break
            await asyncio.sleep(0.005)
        assert readiness.leases == 0  # lease released on cancellation
        assert store.get(a.id).status is JobStatus.cancelled
    finally:
        await runner.stop()


async def test_not_ready_job_takes_no_lease() -> None:
    # A not-ready seam fails the job before the engine call, so no lease is taken (the lease wraps only the
    # actual generation).
    engine = FakeEngineClient(default="succeed")
    readiness = FakeReadiness(ReadinessState.WARMING)
    runner, store = _runner(engine, readiness)
    runner.start()
    try:
        a = await runner.submit(_SUB)
        await _wait_terminal(store, a.id)
        assert store.get(a.id).status is JobStatus.failed
        assert readiness.max_leases == 0  # never leased
        assert engine.calls == 0
    finally:
        await runner.stop()


async def test_not_ready_job_error_includes_the_cause() -> None:
    # AR-1: the runner surfaces the readiness failure cause in the job error, so a caller can tell a
    # timeout from an absent container from a socket denial without operator log access.
    engine = FakeEngineClient(default="succeed")
    readiness = FakeReadiness(ReadinessState.UNAVAILABLE, cause="lifecycle controller unavailable: no socket")
    runner, store = _runner(engine, readiness)
    runner.start()
    try:
        a = await runner.submit(_SUB)
        await _wait_terminal(store, a.id)
        record = store.get(a.id)
        assert record.status is JobStatus.failed
        assert "no socket" in (record.error or "")  # the cause, not just "engine not ready: unavailable"
    finally:
        await runner.stop()


async def test_health_route_surfaces_engine_cause() -> None:
    # AR-1: /health exposes the warming state WITH its cause (the passive status surface S6 renders).
    readiness = FakeReadiness(ReadinessState.WARMING, cause="engine did not become ready within 180s")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(readiness=readiness)))
    result = await health(request)
    assert result["engine"] == "warming"
    assert "did not become ready" in result["engine_cause"]
