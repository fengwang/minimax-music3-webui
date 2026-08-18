"""Readiness seam tests (spec: readiness-seam.md). CPU-only; no engine reachable."""

import asyncio
import inspect

import httpx

import lifecycle.readiness as readiness_module
from lifecycle.readiness import (
    ProbeOutcome,
    Readiness,
    ReadinessState,
    classify_readiness,
    make_models_probe,
)


def test_classify_readiness_is_pure() -> None:
    assert classify_readiness(ProbeOutcome.MODELS_AVAILABLE) is ReadinessState.READY
    assert classify_readiness(ProbeOutcome.LOADING) is ReadinessState.WARMING
    assert classify_readiness(ProbeOutcome.UNREACHABLE) is ReadinessState.UNAVAILABLE


async def test_ensure_ready_maps_each_outcome() -> None:
    async def ready() -> ProbeOutcome:
        return ProbeOutcome.MODELS_AVAILABLE

    async def loading() -> ProbeOutcome:
        return ProbeOutcome.LOADING

    async def down() -> ProbeOutcome:
        return ProbeOutcome.UNREACHABLE

    assert await Readiness(ready).ensure_ready() is ReadinessState.READY
    assert await Readiness(loading).ensure_ready() is ReadinessState.WARMING
    assert await Readiness(down).ensure_ready() is ReadinessState.UNAVAILABLE


async def test_concurrent_calls_collapse_into_one_probe() -> None:
    calls = 0

    async def slow_probe() -> ProbeOutcome:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return ProbeOutcome.MODELS_AVAILABLE

    readiness = Readiness(slow_probe, cache_seconds=60.0)
    results = await asyncio.gather(*[readiness.ensure_ready() for _ in range(5)])
    assert calls == 1
    assert all(state is ReadinessState.READY for state in results)


async def test_cache_reused_within_window() -> None:
    calls = 0

    async def probe() -> ProbeOutcome:
        nonlocal calls
        calls += 1
        return ProbeOutcome.MODELS_AVAILABLE

    readiness = Readiness(probe, cache_seconds=60.0)
    await readiness.ensure_ready()
    await readiness.ensure_ready()
    assert calls == 1
    assert readiness.last_known() is ReadinessState.READY


async def test_cache_expires_and_reprobes() -> None:
    calls = 0
    clock = [0.0]

    async def probe() -> ProbeOutcome:
        nonlocal calls
        calls += 1
        return ProbeOutcome.MODELS_AVAILABLE

    readiness = Readiness(probe, cache_seconds=5.0, clock=lambda: clock[0])
    await readiness.ensure_ready()
    clock[0] = 100.0
    await readiness.ensure_ready()
    assert calls == 2


async def test_http_probe_classifies_via_mock_transport() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": []})

    def loading(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    base = "http://sglang:8000"
    assert await make_models_probe(base, 5.0, transport=httpx.MockTransport(ok))() is ProbeOutcome.MODELS_AVAILABLE
    assert await make_models_probe(base, 5.0, transport=httpx.MockTransport(loading))() is ProbeOutcome.LOADING
    assert await make_models_probe(base, 5.0, transport=httpx.MockTransport(down))() is ProbeOutcome.UNREACHABLE


def test_module_polls_models_only_never_health() -> None:
    source = inspect.getsource(readiness_module)
    assert "/v1/models" in source  # the only readiness signal lives here
    assert "/health" not in source  # never poll the engine's /health (E-15, R-16)
    assert "docker" not in source.lower()  # no docker.sock knowledge in S2 (GATE-S5)
