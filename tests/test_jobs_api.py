"""Native jobs API tests (spec: jobs-api.md; adversarial cases 4,5,6,8). CPU-only, fake engine."""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routes.jobs import _event_stream, active_stream_count, router
from fakes import FakeEngineClient, FakeReadiness
from jobs.runner import JobRunner
from jobs.store import JobStatus, JobStore, Submission

_VALID = {"input": "[Verse]\nhi", "instructions": "warm caption", "seed": 0, "max_new_tokens": 250}


def _make_app(engine: FakeEngineClient, *, heartbeat: float = 15.0) -> tuple[FastAPI, JobStore, JobRunner]:
    app = FastAPI()
    store = JobStore()
    readiness = FakeReadiness()
    runner = JobRunner(
        store, engine, model="MiniMaxAI/MiniMax-Music3", clock=lambda: "t", readiness=readiness,
    )
    app.state.store = store
    app.state.runner = runner
    app.state.readiness = readiness
    app.state.config = SimpleNamespace(sse_heartbeat_seconds=heartbeat)
    app.include_router(router)
    return app, store, runner


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _wait_terminal(store: JobStore, job_id: str, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    terminal = {JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled}
    while loop.time() < deadline:
        if store.get(job_id).status in terminal:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("job never reached terminal")


async def test_submit_valid_returns_202_queued() -> None:
    engine = FakeEngineClient(default="hang")
    app, _store, runner = _make_app(engine)
    runner.start()
    try:
        async with _client(app) as client:
            resp = await client.post("/jobs", json=_VALID)
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "queued" and body["id"]
    finally:
        engine.release()
        await runner.stop()


async def test_submit_over_ceiling_is_422_and_engine_uncalled() -> None:
    engine = FakeEngineClient(default="succeed")
    app, _store, runner = _make_app(engine)
    runner.start()
    try:
        async with _client(app) as client:
            resp = await client.post("/jobs", json={**_VALID, "max_new_tokens": 9001})
        assert resp.status_code == 422
        await asyncio.sleep(0.02)
        assert engine.calls == 0  # refused at the boundary, never reached the engine
    finally:
        await runner.stop()


async def test_submit_rejects_unsupported_and_cloud_fields() -> None:
    # M1: native fields only — a cloud/unsupported field is refused (422), never silently accepted.
    engine = FakeEngineClient(default="succeed")
    app, _store, _runner = _make_app(engine)  # no worker: all requests are rejected before the queue
    extras = ({"model": "music-3.0"}, {"audio_setting": {"format": "mp3"}}, {"stream": True}, {"prompt": "x"})
    async with _client(app) as client:
        for extra in extras:
            resp = await client.post("/jobs", json={**_VALID, **extra})
            assert resp.status_code == 422, f"expected 422 for extra field {extra}"
    assert engine.calls == 0


async def test_submit_missing_field_is_422() -> None:
    engine = FakeEngineClient(default="succeed")
    app, _store, runner = _make_app(engine)
    runner.start()
    try:
        async with _client(app) as client:
            resp = await client.post("/jobs", json={"instructions": "c", "max_new_tokens": 250})
        assert resp.status_code == 422
    finally:
        await runner.stop()


async def test_get_unknown_is_404_and_known_returns_status() -> None:
    engine = FakeEngineClient(default="hang")
    app, _store, runner = _make_app(engine)
    runner.start()
    try:
        async with _client(app) as client:
            assert (await client.get("/jobs/nope")).status_code == 404
            submitted = (await client.post("/jobs", json=_VALID)).json()
            got = await client.get(f"/jobs/{submitted['id']}")
        assert got.status_code == 200
        assert got.json()["params"]["max_new_tokens"] == 250
    finally:
        engine.release()
        await runner.stop()


async def test_cancel_endpoint_status_codes() -> None:
    # Exercises the HTTP cancel surface (404 / 200 / 409) against a *queued* job. The worker is left
    # unstarted so no in-flight engine call is cancelled over ASGITransport. The running-cancel /
    # no-orphan behaviour is proven directly in test_runner.py::test_cancel_running_leaves_no_orphan.
    engine = FakeEngineClient(default="succeed")
    app, _store, _runner = _make_app(engine)
    async with _client(app) as client:
        assert (await client.post("/jobs/nope/cancel")).status_code == 404
        queued = (await client.post("/jobs", json=_VALID)).json()
        assert queued["status"] == "queued"
        cancelled = await client.post(f"/jobs/{queued['id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        again = await client.post(f"/jobs/{queued['id']}/cancel")
        assert again.status_code == 409  # already terminal -> conflict


async def _collect_sse(app: FastAPI, path: str, headers: dict | None = None, limit: float = 1.0) -> list[str]:
    lines: list[str] = []
    async with _client(app) as client, client.stream("GET", path, headers=headers) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        try:
            async with asyncio.timeout(limit):
                async for line in resp.aiter_lines():
                    lines.append(line)
        except TimeoutError:
            pass
    return lines


async def test_sse_late_listener_receives_terminal() -> None:
    engine = FakeEngineClient(default="succeed")
    app, store, runner = _make_app(engine)
    runner.start()
    try:
        async with _client(app) as client:
            job = (await client.post("/jobs", json=_VALID)).json()
        await _wait_terminal(store, job["id"])  # completes with no listener attached
        lines = await _collect_sse(app, f"/jobs/{job['id']}/events")
        assert any("event: succeeded" in line for line in lines)  # terminal not lost
    finally:
        await runner.stop()


async def test_sse_reconnect_delta_and_binding() -> None:
    engine = FakeEngineClient(default="succeed")
    app, store, runner = _make_app(engine)
    runner.start()
    try:
        async with _client(app) as client:
            a = (await client.post("/jobs", json=_VALID)).json()
            b = (await client.post("/jobs", json=_VALID)).json()
        await _wait_terminal(store, a["id"])
        await _wait_terminal(store, b["id"])
        # reconnect on A after id=1 -> only ids 2,3 of A
        a_lines = await _collect_sse(app, f"/jobs/{a['id']}/events", headers={"Last-Event-ID": "1"})
        assert any("id: 2" in line for line in a_lines)
        assert any("id: 3" in line for line in a_lines)
        assert not any(line.strip() == "id: 1" for line in a_lines)
        # B's stream is its own three events only (binding: per-job log)
        b_lines = await _collect_sse(app, f"/jobs/{b['id']}/events")
        assert sum(1 for line in b_lines if line.startswith("event: ")) == 3
    finally:
        await runner.stop()


async def test_sse_events_unknown_job_404() -> None:
    engine = FakeEngineClient(default="succeed")
    app, _store, runner = _make_app(engine)
    runner.start()
    try:
        async with _client(app) as client:
            resp = await client.get("/jobs/nope/events")
        assert resp.status_code == 404
    finally:
        await runner.stop()


class _DisconnectingRequest:
    """A minimal Request stand-in: connected on the first poll, disconnected thereafter."""

    def __init__(self) -> None:
        self._checks = 0

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > 1


async def test_sse_generator_cleans_up_no_leak() -> None:
    # ASGITransport does not deliver http.disconnect reliably, so drive the generator directly:
    # a non-terminal job's stream would tail forever unless the disconnect path fires the finally.
    store = JobStore()
    store.create(Submission(input="i", instructions="c", seed=0, max_new_tokens=250), now="t", job_id="J")
    store.append_event("J", "running", {})  # non-terminal
    assert active_stream_count() == 0
    for _ in range(5):
        chunks = [chunk async for chunk in _event_stream(_DisconnectingRequest(), store, "J", None, 15.0)]
        assert any("event: running" in chunk for chunk in chunks)
    assert active_stream_count() == 0  # the finally ran every time -> no consumer leak
