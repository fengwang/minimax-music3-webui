"""In-memory job records and their pure state transitions.

No database (D6). **R-18: in-flight jobs are lost across a process restart** — this is accepted for this
phase and documented here rather than engineered around. Finished artifacts are S3's concern and will live
on disk; the artifact list is rebuilt from the filesystem, not from this store. The store shape (records +
an atomic ``update``) lets a later durable implementation replace it without changing callers.

ACD: ``Submission``/``JobRecord`` are immutable Data; ``to_*`` are pure Calculations (the wall-clock instant
is passed in, never read here); ``JobStore`` is the thin Action shell holding mutable state.
"""

import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum

from engines.sglang.client import EngineResult
from jobs.events import JobEvent


class JobStatus(str, Enum):
    """Lifecycle states. ``queued`` and ``running`` are non-terminal; the rest are terminal."""

    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


_TERMINAL = frozenset({JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled})


@dataclass(frozen=True)
class Submission:
    """The validated native fields of one generation request.

    ``cloud`` (A-S4-01, additive) carries the received cloud envelope when the submission came through the
    cloud edge, so ``build_sidecar`` can record it (INV-9 for S4). Native ``/jobs`` submissions leave it
    ``None``: the sidecar's ``request`` object is then exactly S3's (no ``cloud`` key), and the sidecar gains
    only the additive top-level ``trace_id`` — the native path's behaviour is unchanged.
    """

    input: str
    instructions: str
    seed: int
    max_new_tokens: int
    cloud: Mapping[str, object] | None = None


@dataclass(frozen=True)
class JobRecord:
    """One job's full record: params, timings, engine identity, result bytes, and error.

    Carries everything S3's sidecar JSON needs (INV-9) so artifacts can be written without engine access.
    """

    id: str
    submission: Submission
    status: JobStatus
    submitted_at: str
    started_at: str | None = None
    ended_at: str | None = None
    engine: str | None = None
    model: str | None = None
    audio: bytes | None = None
    content_type: str | None = None
    generation_seconds: float | None = None
    error: str | None = None


class JobNotFound(Exception):
    """Raised by ``JobStore.get``/``update`` for an unknown job id (maps to HTTP 404 at the edge)."""


class JobTransitionError(RuntimeError):
    """Raised when a lifecycle transition is applied to an illegal source status (maps to HTTP 409)."""

    def __init__(self, from_status: JobStatus, to_status: JobStatus) -> None:
        super().__init__(f"illegal transition {from_status.value} -> {to_status.value}")
        self.from_status = from_status
        self.to_status = to_status


def to_running(job: JobRecord, now: str) -> JobRecord:
    """queued -> running, stamping ``started_at``. Pure."""
    if job.status is not JobStatus.queued:
        raise JobTransitionError(job.status, JobStatus.running)
    return replace(job, status=JobStatus.running, started_at=now)


def to_succeeded(job: JobRecord, *, result: EngineResult, now: str) -> JobRecord:
    """running -> succeeded, recording the engine result and ``ended_at``. Pure."""
    if job.status is not JobStatus.running:
        raise JobTransitionError(job.status, JobStatus.succeeded)
    return replace(
        job,
        status=JobStatus.succeeded,
        ended_at=now,
        engine=result.engine,
        model=result.model,
        audio=result.audio,
        content_type=result.content_type,
        generation_seconds=result.generation_seconds,
    )


def to_failed(job: JobRecord, *, error: str, now: str) -> JobRecord:
    """non-terminal -> failed, recording the error and ``ended_at``. Pure."""
    if job.status in _TERMINAL:
        raise JobTransitionError(job.status, JobStatus.failed)
    return replace(job, status=JobStatus.failed, ended_at=now, error=error)


def to_cancelled(job: JobRecord, *, now: str) -> JobRecord:
    """non-terminal -> cancelled, stamping ``ended_at``. Pure."""
    if job.status in _TERMINAL:
        raise JobTransitionError(job.status, JobStatus.cancelled)
    return replace(job, status=JobStatus.cancelled, ended_at=now)


class JobStore:
    """In-memory record store. Synchronous, non-awaiting methods so each is atomic under the event loop."""

    def __init__(self, event_log_limit: int = 1024) -> None:
        if event_log_limit < 1:
            # A 0-length deque would drop the terminal event, losing it for late listeners and leaving
            # SSE streams tailing forever (spec: progress-events.md). Reject rather than corrupt.
            raise ValueError(f"event_log_limit must be >= 1; got {event_log_limit}")
        self._jobs: dict[str, JobRecord] = {}
        self._events: dict[str, deque[JobEvent]] = {}
        self._seq: dict[str, int] = {}
        self._event_log_limit = event_log_limit

    def create(self, submission: Submission, now: str, job_id: str | None = None) -> JobRecord:
        """Create and store a fresh ``queued`` record; generates a job id when one is not supplied."""
        job_id = job_id or uuid.uuid4().hex
        record = JobRecord(id=job_id, submission=submission, status=JobStatus.queued, submitted_at=now)
        self._jobs[job_id] = record
        self._events[job_id] = deque(maxlen=self._event_log_limit)
        self._seq[job_id] = 0
        return record

    def get(self, job_id: str) -> JobRecord:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise JobNotFound(job_id) from None

    def update(self, job_id: str, fn) -> JobRecord:
        """Read the record, apply the pure transition ``fn``, store, and return it — one atomic step."""
        updated = fn(self.get(job_id))
        self._jobs[job_id] = updated
        return updated

    def append_event(self, job_id: str, event_type: str, data: dict | None = None) -> JobEvent:
        """Append a progress event to the job's retained log with the next per-job monotonic id."""
        if job_id not in self._events:
            raise JobNotFound(job_id)
        self._seq[job_id] += 1
        event = JobEvent(id=self._seq[job_id], type=event_type, data=dict(data or {}))
        self._events[job_id].append(event)
        return event

    def log(self, job_id: str) -> tuple[JobEvent, ...]:
        """Return a snapshot of the job's retained event log (oldest to newest)."""
        if job_id not in self._events:
            raise JobNotFound(job_id)
        return tuple(self._events[job_id])
