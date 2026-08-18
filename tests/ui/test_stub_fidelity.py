"""Seam fidelity: the stub surface is a strict SUBSET of ledger §4 and serves the page like production.

Uses fastapi.testclient.TestClient (no browser), so these run fast and assert the raw HTTP surface —
including the SSE frame format and the heartbeat COMMENT a browser EventSource would hide. Lives under
tests/ui/ so it is part of the ui suite (auto-marked `ui`, gated on Playwright presence), but it launches
no browser and therefore never skips for a missing one.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient
from stub_backend import (
    ENGINE_STATES,
    SSE_EVENT_NAMES,
    create_stub_app,
    make_listing_item,
    make_sidecar,
)

_APP_JS = Path(__file__).resolve().parents[2] / "api" / "app" / "static" / "app.js"
_FORBIDDEN_KEYS = {
    "progress", "percent", "percentage", "eta", "queue_depth", "queue_position",
    "step", "steps", "token_count", "tokens_generated",
}

# Positive anti-drift allowlist: the exact §4 field sets (api/jobs/artifacts.py scan_listing + sidecar;
# api/app/routes/jobs.py _job_view). The stub must EQUAL these, so a later session that adds or drops a
# field fails and must update this expectation deliberately.
_EXPECTED_LISTING_KEYS = {"job_id", "audio_url", "sidecar_url", "mtime", "engine", "model", "output", "timings"}
_EXPECTED_SIDECAR_KEYS = {
    "sidecar_version", "job_id", "trace_id", "engine", "model", "request", "timings", "output",
}
_EXPECTED_OUTPUT_KEYS = {
    "sample_rate", "channels", "duration_seconds", "byte_size", "content_hash", "content_type",
}
_EXPECTED_REQUEST_KEYS = {"input", "instructions", "seed", "max_new_tokens", "translated"}
_EXPECTED_ENGINE_KEYS = {"name", "version", "image_digest"}
_EXPECTED_MODEL_KEYS = {"id", "path"}
_EXPECTED_TIMINGS_KEYS = {
    "submitted_at", "started_at", "ended_at", "generation_seconds", "queue_seconds", "total_seconds",
}
_EXPECTED_JOB_VIEW_KEYS = {
    "id", "status", "params", "submitted_at", "started_at", "ended_at", "engine", "model", "error",
}


def _all_keys(blob) -> set:
    keys: set = set()
    if isinstance(blob, dict):
        for key, value in blob.items():
            keys.add(key)
            keys |= _all_keys(value)
    elif isinstance(blob, list):
        for item in blob:
            keys |= _all_keys(item)
    return keys


def test_sse_event_names_equal_the_client_set():
    text = _APP_JS.read_text(encoding="utf-8")
    match = re.search(r"for \(const type of \[([^\]]*)\]\)", text)
    assert match, "could not find the client's SSE event registration near app.js:147"
    client_names = tuple(re.findall(r'"([a-z]+)"', match.group(1)))
    assert client_names == SSE_EVENT_NAMES
    assert SSE_EVENT_NAMES == ("queued", "running", "succeeded", "failed", "cancelled")


def test_sse_frames_render_like_the_backend():
    app = create_stub_app()
    app.state.stub.events = [("queued", {}), ("running", {}), ("succeeded", {})]
    with TestClient(app) as client, client.stream("GET", "/jobs/job-demo/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    assert "id: 1\nevent: queued\ndata: {}\n\n" in body
    assert "id: 2\nevent: running\ndata: {}\n\n" in body
    assert "event: succeeded" in body
    assert "event: heartbeat" not in body  # a heartbeat is never a named event


def test_failed_carries_error_others_empty():
    app = create_stub_app()
    app.state.stub.events = [("running", {}), ("failed", {"error": "engine oom"})]
    with TestClient(app) as client, client.stream("GET", "/jobs/j/events") as response:
        body = "".join(response.iter_text())
    assert "event: running\ndata: {}\n\n" in body
    assert '"error": "engine oom"' in body and "event: failed" in body


def test_heartbeat_is_an_sse_comment():
    app = create_stub_app()
    app.state.stub.events = []  # no terminal -> a single heartbeat comment, then close
    with TestClient(app) as client, client.stream("GET", "/jobs/j/events") as response:
        body = "".join(response.iter_text())
    assert ": heartbeat\n\n" in body
    assert "event: heartbeat" not in body


def test_health_reports_only_section4_states():
    app = create_stub_app()
    with TestClient(app) as client:
        for state in ENGINE_STATES:
            app.state.stub.engine = state
            payload = client.get("/health").json()
            assert set(payload) == {"status", "engine", "engine_cause"}
            assert payload["engine"] == state
    assert ENGINE_STATES == ("ready", "warming", "unavailable")


def test_stub_invents_no_progress_field():
    present = _all_keys(make_listing_item()) | _all_keys(make_sidecar())
    leaked = present & _FORBIDDEN_KEYS
    assert not leaked, f"stub leaked a non-§4 progress-like field: {leaked}"


def test_root_serves_index_and_api_precedes_catch_all():
    app = create_stub_app()
    with TestClient(app) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "MiniMax-Music3" in root.text and 'id="generate"' in root.text
        # A stubbed API path resolves from its own route, not the static catch-all.
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/style.css").status_code == 200


def test_stub_fields_equal_the_section4_set():
    """Positive anti-drift allowlist: the stub's field sets EQUAL §4 exactly (not merely omit a progress
    blacklist), so a later session that adds OR drops a field fails here and must update the expectation
    deliberately. Stub drift is a named failure mode (adversarial case #5)."""
    item, sidecar = make_listing_item(), make_sidecar()
    assert set(item) == _EXPECTED_LISTING_KEYS
    assert set(sidecar) == _EXPECTED_SIDECAR_KEYS
    assert set(item["output"]) == _EXPECTED_OUTPUT_KEYS == set(sidecar["output"])
    assert set(sidecar["request"]) == _EXPECTED_REQUEST_KEYS
    assert set(item["engine"]) == _EXPECTED_ENGINE_KEYS == set(sidecar["engine"])
    assert set(item["model"]) == _EXPECTED_MODEL_KEYS == set(sidecar["model"])
    assert set(item["timings"]) == _EXPECTED_TIMINGS_KEYS == set(sidecar["timings"])


def test_post_jobs_matches_job_view_shape():
    """POST /jobs returns exactly the _job_view surface (jobs.py:54-71) the client consumes at app.js:239,
    with a 202 and the submitted params echoed — no field the frozen backend does not have."""
    app = create_stub_app()
    with TestClient(app) as client:
        response = client.post(
            "/jobs", json={"input": "x", "instructions": "y", "seed": 3, "max_new_tokens": 100}
        )
    assert response.status_code == 202
    body = response.json()
    assert set(body) == _EXPECTED_JOB_VIEW_KEYS
    assert set(body["params"]) == {"input", "instructions", "seed", "max_new_tokens"}
    assert body["params"] == {"input": "x", "instructions": "y", "seed": 3, "max_new_tokens": 100}
    assert body["status"] == "queued"
