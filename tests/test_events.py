"""Progress-event calcs + store event log (spec: progress-events.md). CPU-only."""

import pytest

from jobs.events import TERMINAL_EVENT_TYPES, JobEvent, events_since, render_sse
from jobs.store import JobNotFound, JobStore, Submission

_SUB = Submission(input="i", instructions="c", seed=0, max_new_tokens=250)


def test_events_since_selects_by_id() -> None:
    log = [JobEvent(1, "queued", {}), JobEvent(2, "running", {}), JobEvent(3, "succeeded", {})]
    assert [e.id for e in events_since(log, None)] == [1, 2, 3]
    assert [e.id for e in events_since(log, 1)] == [2, 3]
    assert events_since(log, 3) == []


def test_render_sse_framing() -> None:
    assert render_sse(JobEvent(3, "succeeded", {})) == "id: 3\nevent: succeeded\ndata: {}\n\n"
    assert render_sse(JobEvent(2, "running", {"foo": "bar"})) == (
        'id: 2\nevent: running\ndata: {"foo": "bar"}\n\n'
    )


def test_terminal_event_types() -> None:
    assert TERMINAL_EVENT_TYPES == frozenset({"succeeded", "failed", "cancelled"})


def test_store_append_assigns_monotonic_ids_per_job() -> None:
    s = JobStore()
    s.create(_SUB, now="t0", job_id="A")
    s.create(_SUB, now="t0", job_id="B")
    a1 = s.append_event("A", "queued", {})
    a2 = s.append_event("A", "running", {})
    b1 = s.append_event("B", "queued", {})
    assert (a1.id, a2.id) == (1, 2)
    assert b1.id == 1  # per-job sequence, independent of A
    assert [e.id for e in s.log("A")] == [1, 2]


def test_store_log_unknown_raises() -> None:
    with pytest.raises(JobNotFound):
        JobStore().log("nope")


def test_event_log_is_bounded_keeping_newest() -> None:
    s = JobStore(event_log_limit=3)
    s.create(_SUB, now="t0", job_id="A")
    for i in range(5):
        s.append_event("A", "running", {"i": i})
    log = s.log("A")
    assert len(log) == 3
    assert [e.data["i"] for e in log] == [2, 3, 4]  # oldest dropped, ids stay monotonic
    assert [e.id for e in log] == [3, 4, 5]
