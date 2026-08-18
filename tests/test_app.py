"""App composition, config, health, and lifespan tests (spec: app-lifecycle.md). CPU-only."""

import asyncio
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import config_from_env, create_app, startup_banner
from fakes import FakeEngineClient
from jobs.store import JobStore
from lifecycle.readiness import ProbeOutcome, Readiness

_VALID = {"input": "[Verse]\nhi", "instructions": "warm caption", "seed": 0, "max_new_tokens": 250}


async def _ready_probe() -> ProbeOutcome:
    return ProbeOutcome.MODELS_AVAILABLE


def _app() -> object:
    return create_app(
        config=config_from_env({}),
        engine=FakeEngineClient(default="succeed"),
        readiness=Readiness(_ready_probe),
    )


def test_config_defaults() -> None:
    c = config_from_env({})
    assert c.bind_addr == "0.0.0.0"
    assert c.app_port == 8080
    assert c.engine_base_url == "http://sglang:8000"
    assert c.engine_model == "MiniMaxAI/MiniMax-Music3"
    assert c.engine_read_timeout_seconds == 1200.0


def test_config_overrides() -> None:
    c = config_from_env({"MUSIC3_BIND_ADDR": "127.0.0.1", "MUSIC3_APP_PORT": "9000"})
    assert c.bind_addr == "127.0.0.1"
    assert c.app_port == 9000


def test_startup_banner_states_bind_and_unauthenticated() -> None:
    banner = startup_banner(config_from_env({"MUSIC3_BIND_ADDR": "127.0.0.1"}))
    assert "127.0.0.1" in banner
    assert "unauthenticated" in banner.lower()


async def test_startup_logs_bind_and_unauthenticated_inv13(caplog) -> None:
    app = _app()
    with caplog.at_level(logging.WARNING):
        async with app.router.lifespan_context(app):
            pass
    assert "0.0.0.0" in caplog.text
    assert "unauthenticated" in caplog.text.lower()


class _CountingProbe:
    """A readiness probe that counts invocations, to prove who triggers ``ensure_ready``."""

    def __init__(self, outcome: ProbeOutcome = ProbeOutcome.MODELS_AVAILABLE) -> None:
        self.calls = 0
        self.outcome = outcome

    async def __call__(self) -> ProbeOutcome:
        self.calls += 1
        return self.outcome


async def test_health_is_passive_last_known_and_worker_is_sole_prober() -> None:
    # M2 + H1: /health reports last-known state and NEVER probes (a probe would wake the GPU under S5);
    # the worker is the sole trigger of ensure_ready(), so a job submission is what primes readiness.
    probe = _CountingProbe()
    app = create_app(
        config=config_from_env({}),
        engine=FakeEngineClient(default="succeed"),
        readiness=Readiness(probe),
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        first = await client.get("/health")
        assert first.status_code == 200
        assert first.json()["engine"] == "warming"  # last-known before any observation
        assert probe.calls == 0  # health did not probe

        job = (await client.post("/jobs", json=_VALID)).json()
        status = None
        for _ in range(400):
            status = (await client.get(f"/jobs/{job['id']}")).json()["status"]
            if status in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.005)
        assert status == "succeeded"
        assert probe.calls >= 1  # the worker gated on readiness

        calls_after_job = probe.calls
        second = await client.get("/health")
        assert second.json()["engine"] == "ready"  # health reflects the worker's observation
        assert probe.calls == calls_after_job  # health still added no probe


async def test_app_end_to_end_submit_succeeds() -> None:
    app = _app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        job = (await client.post("/jobs", json=_VALID)).json()
        assert job["status"] == "queued"
        status = None
        for _ in range(400):
            status = (await client.get(f"/jobs/{job['id']}")).json()["status"]
            if status in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.005)
    assert status == "succeeded"


def test_config_rejects_zero_event_log_limit() -> None:
    # M3: a zero-length event log would drop the terminal SSE event; reject at parse time.
    with pytest.raises(ValueError):
        config_from_env({"MUSIC3_EVENT_LOG_LIMIT": "0"})


def test_store_rejects_nonpositive_event_log_limit() -> None:
    # M3: defensive rejection at the store boundary as well.
    with pytest.raises(ValueError):
        JobStore(event_log_limit=0)
