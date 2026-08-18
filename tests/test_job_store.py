"""Job store + pure transitions tests (spec: job-store.md). CPU-only."""

import pytest

import jobs.store as store_module
from engines.sglang.client import EngineResult
from jobs.store import (
    JobNotFound,
    JobStatus,
    JobStore,
    JobTransitionError,
    Submission,
    to_cancelled,
    to_failed,
    to_running,
    to_succeeded,
)

_SUB = Submission(input="[Verse]\nhi", instructions="warm caption", seed=7, max_new_tokens=250)
_RESULT = EngineResult(
    audio=b"RIFFwav", content_type="audio/wav", engine="sglang-omni",
    model="MiniMaxAI/MiniMax-Music3", generation_seconds=12.5,
)


def _queued() -> object:
    return JobStore().create(_SUB, now="t0", job_id="job-1")


def test_to_running_is_pure_and_sets_started_at() -> None:
    q = _queued()
    r = to_running(q, now="t1")
    assert r.status is JobStatus.running
    assert r.started_at == "t1"
    assert q.status is JobStatus.queued  # original untouched (immutability)
    assert q.started_at is None


def test_illegal_transition_from_terminal_raises() -> None:
    done = to_succeeded(to_running(_queued(), now="t1"), result=_RESULT, now="t2")
    assert done.status is JobStatus.succeeded
    with pytest.raises(JobTransitionError):
        to_running(done, now="t3")
    with pytest.raises(JobTransitionError):
        to_cancelled(done, now="t3")


def test_completed_record_carries_provenance_for_s3() -> None:
    done = to_succeeded(to_running(_queued(), now="t1"), result=_RESULT, now="t2")
    assert done.submission == _SUB
    assert done.submission.seed == 7
    assert done.engine == "sglang-omni"
    assert done.model == "MiniMaxAI/MiniMax-Music3"
    assert done.audio == b"RIFFwav"
    assert done.submitted_at == "t0" and done.started_at == "t1" and done.ended_at == "t2"


def test_failure_records_error_and_is_terminal() -> None:
    failed = to_failed(to_running(_queued(), now="t1"), error="boom", now="t2")
    assert failed.status is JobStatus.failed
    assert failed.error == "boom"
    assert failed.ended_at == "t2"


def test_cancel_allowed_from_queued_and_running() -> None:
    assert to_cancelled(_queued(), now="t1").status is JobStatus.cancelled
    assert to_cancelled(to_running(_queued(), now="t1"), now="t2").status is JobStatus.cancelled


def test_store_update_applies_transition_atomically() -> None:
    s = JobStore()
    rec = s.create(_SUB, now="t0", job_id="job-9")
    assert rec.status is JobStatus.queued
    s.update("job-9", lambda r: to_running(r, now="t1"))
    assert s.get("job-9").status is JobStatus.running


def test_get_unknown_raises_not_found() -> None:
    with pytest.raises(JobNotFound):
        JobStore().get("nope")


def test_restart_loss_is_documented() -> None:
    doc = (store_module.__doc__ or "").lower()
    assert "r-18" in doc and "restart" in doc
