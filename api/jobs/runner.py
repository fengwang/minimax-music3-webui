"""The single-slot job runner: one queue, one worker coroutine, concurrency exactly one (INV-1).

The worker pulls one job id at a time and awaits the engine call before pulling the next, so two
submissions cannot both reach the engine. The engine call runs as a *child task* so ``cancel()`` can abort
just that call (dropping the engine connection) without killing the worker loop — that is the
cancel-no-orphan mechanism. A per-job ``finally`` frees the slot on success, failure, or cancel, so a
failed generation never deadlocks the queue and the next job runs. Before generating, the worker gates on
the ``ensure_ready()`` seam — a not-ready result fails the job without touching the engine — so S5's
cold-start/warm-up slots in behind the unchanged seam (health never calls it; the worker is the sole
trigger, so a health check cannot wake the GPU).

ACD: this is the Action/orchestration shell. It composes the pure store transitions and event calcs; the
wall-clock instant and engine are injected. ``cancel()`` performs no ``await`` and there is no ``await``
between the cancelled-check and the transition to ``running``, so a cancel can never be lost or resurrected.
Both terminal branches re-check the cancelled set before transitioning, so a cancel that lands in the race
window after the engine call resolves but before the worker resumes leaves the record ``cancelled`` rather
than raising; a ``JobTransitionError`` net in ``_loop`` keeps the single worker alive against any unforeseen
transition race.
"""

import asyncio
import logging
import wave
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from engines.sglang.client import EngineClient, EngineError, EngineRequest, build_body
from jobs import artifacts
from jobs.store import (
    JobRecord,
    JobStatus,
    JobStore,
    JobTransitionError,
    Submission,
    to_cancelled,
    to_failed,
    to_running,
    to_succeeded,
)
from lifecycle.readiness import ReadinessState

_log = logging.getLogger(__name__)
_TERMINAL = frozenset({JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled})


class ReadinessGate(Protocol):
    """The narrow readiness dependency the worker gates on before each generation. Implemented by
    ``lifecycle.readiness.Readiness``; S5 fills in container start + warm-up behind the unchanged seam.

    ``lease()`` (A-S5-01) is held across the engine call so S5's idle-down can never stop the container
    while a generation is in flight; it takes no argument and is released on success, failure and cancel.
    """

    async def ensure_ready(self) -> ReadinessState: ...

    def lease(self) -> AbstractAsyncContextManager[None]: ...

    def last_cause(self) -> str: ...


class JobRunner:
    """Owns exactly one execution slot.

    Args:
        store: the job store (records + retained event log).
        engine: the injected ``EngineClient`` (real ``SglangClient`` or a fake).
        model: the fixed served model id sent in every engine request.
        clock: returns the current wall-clock instant as a string (injected; stamped into records/timings).
        readiness: the readiness seam the worker gates on before generating (S5 warms up behind it).
        artifacts_root: S3 artifacts root; when set, a successful generation is persisted through
            ``jobs.artifacts.write_artifact`` before the record is marked succeeded. ``None`` disables
            persistence and preserves S2 behaviour exactly.
    """

    def __init__(
        self,
        store: JobStore,
        engine: EngineClient,
        *,
        model: str,
        clock,
        readiness: ReadinessGate,
        artifacts_root: str | None = None,
    ) -> None:
        self._store = store
        self._engine = engine
        self._model = model
        self._clock = clock
        self._readiness = readiness
        self._artifacts_root = artifacts_root
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._cancelled: set[str] = set()
        self._current: str | None = None
        self._current_task: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None

    def start(self) -> None:
        """Start the single worker coroutine. Call once, inside a running event loop."""
        if self._worker is None:
            self._worker = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the worker (and any in-flight engine call) and wait for it to unwind."""
        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None

    async def submit(self, submission: Submission) -> JobRecord:
        """Create a queued job, enqueue it for the single worker, and emit its first event."""
        record = self._store.create(submission, now=self._clock())
        self._store.append_event(record.id, JobStatus.queued.value, {})
        self._queue.put_nowait(record.id)
        return record

    async def cancel(self, job_id: str) -> JobRecord:
        """Cancel a queued or running job. Terminal for the record and any in-flight engine call.

        Raises ``JobNotFound`` (404) for an unknown id and ``JobTransitionError`` (409) if already terminal.
        Performs no ``await``: the ``_cancelled`` guard and the record transition happen atomically.
        """
        record = self._store.get(job_id)
        if record.status in _TERMINAL:
            raise JobTransitionError(record.status, JobStatus.cancelled)
        self._cancelled.add(job_id)  # set before anything else; no await follows
        if record.status is JobStatus.running and self._current == job_id and self._current_task is not None:
            self._current_task.cancel()  # abort the in-flight engine call -> connection dropped
        updated = self._store.update(job_id, lambda r: to_cancelled(r, now=self._clock()))
        self._store.append_event(job_id, JobStatus.cancelled.value, {})
        return updated

    async def _loop(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._process(job_id)
            except JobTransitionError:
                # Defense-in-depth net: a transition race (e.g. cancel landing as the engine finishes)
                # must never wedge the single worker. Specific, not blind — teardown CancelledError
                # still propagates and stops the worker.
                _log.exception("transition race on job %s; worker continues", job_id)
            finally:
                self._queue.task_done()

    async def _process(self, job_id: str) -> None:
        if job_id in self._cancelled:
            self._ensure_cancelled(job_id)  # cancelled while queued; never touch the engine
            return
        # Gate on the readiness seam before generating. In S2 this is a bounded readiness probe; S5
        # implements container start + warm-up behind the unchanged seam. Health never calls this (it would
        # wake the GPU); the worker is the sole trigger, so a job submission is what warms the engine.
        state = await self._readiness.ensure_ready()
        if job_id in self._cancelled:  # a cancel arrived during the readiness probe
            self._ensure_cancelled(job_id)
            return
        if state is not ReadinessState.READY:
            # A-S5-01/AR-1: surface the readiness failure cause in the job error so a caller can tell a
            # timeout from an absent container from a docker-socket denial without operator log access.
            cause = self._readiness.last_cause()
            message = f"engine not ready: {state.value}" + (f" ({cause})" if cause else "")
            _log.warning("job %s not started: %s", job_id, message)
            self._store.update(job_id, lambda r: to_failed(r, error=message, now=self._clock()))
            self._store.append_event(job_id, JobStatus.failed.value, {"error": message})
            return
        # No await between the ready-check above and the transition below: a cancel cannot slip in.
        self._store.update(job_id, lambda r: to_running(r, now=self._clock()))
        self._store.append_event(job_id, JobStatus.running.value, {})
        submission = self._store.get(job_id).submission
        request = EngineRequest(
            model=self._model,
            input=submission.input,
            instructions=submission.instructions,
            seed=submission.seed,
            max_new_tokens=submission.max_new_tokens,
        )
        self._current = job_id
        self._current_task = asyncio.create_task(self._engine.generate(request))
        try:
            # A-S5-01: hold the readiness lease across the engine call so idle-down cannot stop the
            # container mid-generation. Enter/exit take no await, so no cancel can slip between the task
            # creation above and the lease being held; the lease releases on success, failure and cancel.
            async with self._readiness.lease():
                result = await self._current_task
        except asyncio.CancelledError:
            if job_id in self._cancelled:
                self._ensure_cancelled(job_id)  # our cancel() aborted this call
                return
            self._current_task.cancel()  # worker teardown while awaiting the child
            raise
        except EngineError as exc:  # the engine's one documented failure -> terminal failed state
            if job_id in self._cancelled:  # a cancel won the race with the engine failing
                self._ensure_cancelled(job_id)
                return
            message = str(exc)
            _log.warning("job %s failed: %s", job_id, message)
            self._store.update(job_id, lambda r: to_failed(r, error=message, now=self._clock()))
            self._store.append_event(job_id, JobStatus.failed.value, {"error": message})
        else:
            if job_id in self._cancelled:  # a cancel won the race with the engine completing
                self._ensure_cancelled(job_id)
                return
            self._persist_and_succeed(job_id, result, request)
        finally:
            self._current = None
            self._current_task = None

    def _persist_and_succeed(self, job_id: str, result, request: EngineRequest) -> None:
        """Persist the artifact (when a root is configured) and only then mark the job succeeded.

        A write failure fails the job instead, so a success record never exists without its artifact
        (adversarial case 8). The succeeded record is computed purely first and handed to the writer, so
        the sidecar's timings are the ones the record will carry, and a failure leaves the record
        ``running`` → a legal transition to ``failed`` (never an illegal terminal→terminal flip). No
        ``await`` runs here, so a cancel cannot interleave between the write and the transition — the write
        is deliberately synchronous. It briefly stalls the event loop (SSE/health/listing) while it hashes
        and fsyncs the WAV (a full-length song is ~53 MB, not trivial). That stall is accepted rather than
        offloaded to a thread on purpose: an ``await asyncio.to_thread(...)`` here would reopen the S2
        cancel-vs-completion race (a prior High fix) across the whole write and could leave an orphaned
        artifact for a job a late cancel then marks cancelled — changing S2 terminal-state semantics, which
        A-S3-01 forbids. Concurrency is one (INV-1) and the write runs once per multi-minute generation for
        a single owner, so the brief stall is benign; revisit with a thread + cancel-race guard only if a
        slow-storage stall on the other routes is ever observed.
        """
        now = self._clock()
        if self._artifacts_root is not None:
            succeeded = to_succeeded(self._store.get(job_id), result=result, now=now)
            # The engine body the runner sent, recorded verbatim in the sidecar as plain data so the
            # storage module needs no engine import (CR-1). Same pure fn the client used, same request.
            translated = build_body(request)
            try:
                artifacts.write_artifact(succeeded, self._artifacts_root, translated=translated)
            except (OSError, ValueError, wave.Error) as exc:
                message = f"artifact write failed: {exc}"
                _log.warning("job %s: %s", job_id, message)
                self._store.update(job_id, lambda r: to_failed(r, error=message, now=self._clock()))
                self._store.append_event(job_id, JobStatus.failed.value, {"error": message})
                return
        self._store.update(job_id, lambda r: to_succeeded(r, result=result, now=now))
        self._store.append_event(job_id, JobStatus.succeeded.value, {})

    def _ensure_cancelled(self, job_id: str) -> None:
        """Idempotently move a job to cancelled (no-op if ``cancel()`` already did it)."""
        if self._store.get(job_id).status not in _TERMINAL:
            self._store.update(job_id, lambda r: to_cancelled(r, now=self._clock()))
            self._store.append_event(job_id, JobStatus.cancelled.value, {})
