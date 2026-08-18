"""Single-slot runner tests (spec: single-slot-queue.md). CPU-only, fake engine.

Every guarantee is proven by a test against the counting fake, not by reading the code (adversarial
cases 1-3).
"""

import asyncio

import pytest

from engines.sglang.client import EngineRequest, EngineResult
from fakes import FakeEngineClient, FakeReadiness
from jobs.runner import JobRunner
from jobs.store import JobNotFound, JobStatus, JobStore, JobTransitionError, Submission
from lifecycle.readiness import ReadinessState

_SUB = Submission(input="[Verse]\nhi", instructions="warm", seed=0, max_new_tokens=250)


def _runner(engine: FakeEngineClient, readiness: FakeReadiness | None = None) -> tuple[JobRunner, JobStore]:
    store = JobStore()
    runner = JobRunner(
        store, engine, model="MiniMaxAI/MiniMax-Music3", clock=lambda: "t",
        readiness=readiness or FakeReadiness(),
    )
    return runner, store


async def _wait_status(store: JobStore, job_id: str, status: JobStatus, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if store.get(job_id).status is status:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"{job_id} never reached {status}; is {store.get(job_id).status}")


async def _wait_terminal(store: JobStore, job_id: str, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    terminal = {JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled}
    while asyncio.get_running_loop().time() < deadline:
        if store.get(job_id).status in terminal:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"{job_id} never reached a terminal state")


async def test_two_submissions_serialize_inv1() -> None:
    engine = FakeEngineClient(default="hang")
    runner, store = _runner(engine)
    runner.start()
    try:
        a = await runner.submit(_SUB)
        b = await runner.submit(_SUB)
        await _wait_status(store, a.id, JobStatus.running)
        assert engine.active == 1 and engine.max_active == 1
        assert store.get(b.id).status is JobStatus.queued  # B waits, does not race in
        engine.release()
        await _wait_terminal(store, a.id)
        await _wait_terminal(store, b.id)
        assert engine.max_active == 1  # never overlapped
        assert engine.calls == 2
    finally:
        await runner.stop()


async def test_success_records_result_and_events() -> None:
    engine = FakeEngineClient(default="succeed")
    runner, store = _runner(engine)
    runner.start()
    try:
        a = await runner.submit(_SUB)
        await _wait_terminal(store, a.id)
        record = store.get(a.id)
        assert record.status is JobStatus.succeeded
        assert record.audio == b"RIFFwav" and record.engine == "fake"
        assert [e.type for e in store.log(a.id)] == ["queued", "running", "succeeded"]
    finally:
        await runner.stop()


async def test_cancel_running_leaves_no_orphan() -> None:
    engine = FakeEngineClient(default="hang")
    runner, store = _runner(engine)
    runner.start()
    try:
        a = await runner.submit(_SUB)
        await _wait_status(store, a.id, JobStatus.running)
        assert engine.active == 1
        cancelled = await runner.cancel(a.id)
        assert cancelled.status is JobStatus.cancelled
        await _wait_terminal(store, a.id)
        # give the worker a tick to unwind the aborted engine call
        for _ in range(50):
            if engine.active == 0:
                break
            await asyncio.sleep(0.005)
        assert engine.cancelled is True  # the fake observed cancellation
        assert engine.active == 0  # slot free only after the engine call ended
        # slot is free -> a fresh job runs
        b = await runner.submit(_SUB)
        engine.release()
        await _wait_terminal(store, b.id)
        assert store.get(b.id).status is JobStatus.succeeded
    finally:
        await runner.stop()


async def test_cancel_queued_never_reaches_engine() -> None:
    engine = FakeEngineClient(default="hang")
    runner, store = _runner(engine)
    runner.start()
    try:
        a = await runner.submit(_SUB)
        await _wait_status(store, a.id, JobStatus.running)
        b = await runner.submit(_SUB)
        cancelled = await runner.cancel(b.id)  # cancel B while queued behind A
        assert cancelled.status is JobStatus.cancelled
        engine.release()
        await _wait_terminal(store, a.id)
        await _wait_terminal(store, b.id)
        assert store.get(b.id).status is JobStatus.cancelled
        assert engine.calls == 1  # B never reached the engine
    finally:
        await runner.stop()


async def test_cancel_terminal_and_unknown() -> None:
    engine = FakeEngineClient(default="succeed")
    runner, store = _runner(engine)
    runner.start()
    try:
        a = await runner.submit(_SUB)
        await _wait_terminal(store, a.id)
        with pytest.raises(JobTransitionError):
            await runner.cancel(a.id)  # already terminal -> 409
        with pytest.raises(JobNotFound):
            await runner.cancel("nope")  # unknown -> 404
    finally:
        await runner.stop()


async def test_failure_frees_slot_and_next_job_runs() -> None:
    # First engine call raises; the second succeeds.
    engine = FakeEngineClient(behaviors=["raise"], default="succeed")
    runner, store = _runner(engine)
    runner.start()
    try:
        a = await runner.submit(_SUB)
        await _wait_terminal(store, a.id)
        assert store.get(a.id).status is JobStatus.failed
        assert store.get(a.id).error
        b = await runner.submit(_SUB)  # slot must be free
        await _wait_terminal(store, b.id)
        assert store.get(b.id).status is JobStatus.succeeded  # loop survived A's failure
    finally:
        await runner.stop()


class _RaceEngine:
    """Completes and, in the SAME event-loop step, wakes a controller — reproducing the completion/cancel
    window: the engine task is ``done()`` but the worker has not yet resumed from ``await``."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.completing = asyncio.Event()

    async def generate(self, request: EngineRequest) -> EngineResult:
        await self.gate.wait()
        self.completing.set()  # schedules the controller BEFORE this task completes -> it runs first
        return EngineResult(
            audio=b"x", content_type="audio/wav", engine="fake",
            model=request.model, generation_seconds=0.0,
        )


async def test_cancel_racing_engine_completion_does_not_wedge_worker() -> None:
    # Regression for the sharded-review High: a cancel landing after the engine task completes but before
    # the worker resumes must leave the record cancelled and keep the single worker alive (never apply
    # to_succeeded on a cancelled record, which would raise and deadlock the queue forever).
    engine = _RaceEngine()
    store = JobStore()
    runner = JobRunner(
        store, engine, model="MiniMaxAI/MiniMax-Music3", clock=lambda: "t", readiness=FakeReadiness(),
    )
    runner.start()
    try:
        a = await runner.submit(_SUB)
        await _wait_status(store, a.id, JobStatus.running)

        async def _cancel_on_completion() -> None:
            await engine.completing.wait()
            await runner.cancel(a.id)

        controller = asyncio.create_task(_cancel_on_completion())
        engine.gate.set()
        await controller
        await _wait_terminal(store, a.id)
        assert store.get(a.id).status is JobStatus.cancelled  # cancel won the race
        b = await runner.submit(_SUB)  # worker must still be alive
        await _wait_terminal(store, b.id)
        assert store.get(b.id).status is JobStatus.succeeded
    finally:
        await runner.stop()


async def test_worker_gates_on_readiness_before_generating() -> None:
    # H1: the worker calls ensure_ready() before touching the engine.
    engine = FakeEngineClient(default="succeed")
    readiness = FakeReadiness(ReadinessState.READY)
    runner, store = _runner(engine, readiness)
    runner.start()
    try:
        a = await runner.submit(_SUB)
        await _wait_terminal(store, a.id)
        assert store.get(a.id).status is JobStatus.succeeded
        assert readiness.calls >= 1  # gated on the readiness seam
    finally:
        await runner.stop()


async def test_not_ready_fails_job_without_calling_engine() -> None:
    # H1: a not-ready seam fails the job at the boundary and never reaches the engine — so S5's warm-up,
    # implemented behind ensure_ready(), governs whether a job ever reaches the engine.
    for state in (ReadinessState.WARMING, ReadinessState.UNAVAILABLE):
        engine = FakeEngineClient(default="succeed")
        runner, store = _runner(engine, FakeReadiness(state))
        runner.start()
        try:
            a = await runner.submit(_SUB)
            await _wait_terminal(store, a.id)
            record = store.get(a.id)
            assert record.status is JobStatus.failed
            assert "not ready" in (record.error or "")
            assert engine.calls == 0  # never reached the engine
        finally:
            await runner.stop()
