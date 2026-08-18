"""Readiness lifecycle tests (spec: readiness-lifecycle.md). CPU-only; fake controller + scripted probe.

Cold-start-on-demand, one-start coalescing, the bounded readiness timeout, explicit socket-failure
surfacing, and "running container is not ready on status alone" (adversarial cases 4, 5, 7).
"""

import asyncio

import pytest

from fakes import FakeController
from lifecycle.controller import ContainerState, ControllerUnavailable, LifecycleConfig
from lifecycle.readiness import ProbeOutcome, Readiness, ReadinessState

# Fast lifecycle config: idle disabled (3600 s) so the self-armed idle loop never interferes with a
# cold-start assertion; short readiness timeout/poll so a timeout test is sub-second.
_NO_IDLE = LifecycleConfig(socket="x", readiness_timeout_seconds=0.5, idle_timeout_seconds=3600.0, poll_seconds=0.01)


def _probe(outcomes: list[ProbeOutcome]):
    seq = list(outcomes)

    async def probe() -> ProbeOutcome:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return probe


async def test_cold_start_issues_one_start_then_ready() -> None:
    controller = FakeController([ContainerState.STOPPED])
    r = Readiness(
        _probe([ProbeOutcome.UNREACHABLE, ProbeOutcome.MODELS_AVAILABLE]),
        controller=controller,
        lifecycle=_NO_IDLE,
    )
    try:
        assert await r.ensure_ready() is ReadinessState.READY
        assert controller.start_calls == 1
    finally:
        await r.aclose()


async def test_concurrent_cold_calls_issue_exactly_one_start() -> None:
    # Faithful fakes: status()/start()/probe() yield to the loop so five concurrent callers genuinely pile
    # up on the single-flight lock, and readiness is decoupled from start_calls (a separate `started` flag
    # set only after start completes). So this test BITES: with the lock it issues 1 start; delete the lock
    # and the five callers overlap in start() → 5 (mutation-verified).
    started = asyncio.Event()

    class _SlowController:
        def __init__(self) -> None:
            self.start_calls = 0

        async def status(self) -> ContainerState:
            await asyncio.sleep(0)
            return ContainerState.STOPPED

        async def start(self) -> None:
            await asyncio.sleep(0.02)  # a real start takes time; lock-free callers would overlap here
            self.start_calls += 1
            started.set()

        async def stop(self) -> None:
            await asyncio.sleep(0)

    controller = _SlowController()

    async def probe() -> ProbeOutcome:
        await asyncio.sleep(0)
        return ProbeOutcome.MODELS_AVAILABLE if started.is_set() else ProbeOutcome.UNREACHABLE

    r = Readiness(probe, controller=controller, lifecycle=_NO_IDLE)
    try:
        results = await asyncio.gather(*[r.ensure_ready() for _ in range(5)])
        assert controller.start_calls == 1  # single-flight coalescing (INV-1, adversarial case 5)
        assert all(state is ReadinessState.READY for state in results)
    finally:
        await r.aclose()


async def test_running_container_is_not_ready_until_models_2xx() -> None:
    # adversarial case 4: a running container must never be treated as ready on status alone.
    controller = FakeController([ContainerState.RUNNING])
    r = Readiness(
        _probe([ProbeOutcome.LOADING]),
        controller=controller,
        lifecycle=LifecycleConfig("x", 0.1, 3600.0, 0.02),
    )
    try:
        assert await r.ensure_ready() is ReadinessState.WARMING  # running, but /v1/models not 2xx
        assert controller.start_calls == 0  # already running -> no start issued
    finally:
        await r.aclose()


async def test_readiness_timeout_returns_non_ready_with_bound() -> None:
    controller = FakeController([ContainerState.STOPPED])
    r = Readiness(
        _probe([ProbeOutcome.UNREACHABLE, ProbeOutcome.LOADING]),  # starts, then never finishes loading
        controller=controller,
        lifecycle=LifecycleConfig("x", 0.12, 3600.0, 0.02),
    )
    try:
        state = await r.ensure_ready()
        assert state is ReadinessState.WARMING  # gave up at the bound, did not hang
        assert controller.start_calls == 1
    finally:
        await r.aclose()


async def test_socket_failure_during_cold_start_surfaces_unavailable() -> None:
    # adversarial case 7: a missing/denied/unresponsive socket is an explicit failure, never swallowed.
    controller = FakeController(fail=ControllerUnavailable("no socket"))
    r = Readiness(_probe([ProbeOutcome.UNREACHABLE]), controller=controller, lifecycle=_NO_IDLE)
    try:
        assert await r.ensure_ready() is ReadinessState.UNAVAILABLE
    finally:
        await r.aclose()


async def test_no_controller_preserves_s2_probe_only_behaviour() -> None:
    r = Readiness(_probe([ProbeOutcome.LOADING]), controller=None)
    assert await r.ensure_ready() is ReadinessState.WARMING  # reports the probe, starts nothing


async def test_readiness_timeout_bounds_whole_cold_path_including_initial_probe() -> None:
    # CR-3: the deadline must start BEFORE the initial probe. With a probe that "costs" 50 clock units per
    # call and a 100-unit budget, only the initial probe (drive) + one poll fit (2 calls). If the deadline
    # instead started at the poll loop, a third probe would fit before expiry (3 calls).
    t = [0.0]
    calls = [0]

    async def probe() -> ProbeOutcome:
        calls[0] += 1
        t[0] += 50.0
        return ProbeOutcome.UNREACHABLE

    controller = FakeController([ContainerState.STOPPED])
    r = Readiness(
        probe,
        controller=controller,
        lifecycle=LifecycleConfig("x", readiness_timeout_seconds=100.0, idle_timeout_seconds=3600.0, poll_seconds=0.001),
        clock=lambda: t[0],
    )
    try:
        assert await r.ensure_ready() is ReadinessState.UNAVAILABLE
        assert calls[0] == 2  # initial probe + one poll; the initial probe counts against the budget
    finally:
        await r.aclose()


async def test_last_cause_surfaces_the_timeout_reason() -> None:
    controller = FakeController([ContainerState.STOPPED])
    r = Readiness(
        _probe([ProbeOutcome.UNREACHABLE, ProbeOutcome.LOADING]),
        controller=controller,
        lifecycle=LifecycleConfig("x", 0.12, 3600.0, 0.02),
    )
    try:
        assert await r.ensure_ready() is ReadinessState.WARMING
        assert "did not become ready" in r.last_cause()  # a surfaceable cause, not merely the enum (AR-1)
    finally:
        await r.aclose()


async def test_last_cause_surfaces_socket_failure() -> None:
    controller = FakeController(fail=ControllerUnavailable("permission denied"))
    r = Readiness(_probe([ProbeOutcome.UNREACHABLE]), controller=controller, lifecycle=_NO_IDLE)
    try:
        assert await r.ensure_ready() is ReadinessState.UNAVAILABLE
        assert "permission denied" in r.last_cause()  # AR-1: the docker-socket denial is surfaceable
    finally:
        await r.aclose()


async def test_last_cause_cleared_when_ready() -> None:
    controller = FakeController([ContainerState.STOPPED])
    r = Readiness(
        _probe([ProbeOutcome.UNREACHABLE, ProbeOutcome.MODELS_AVAILABLE]),
        controller=controller,
        lifecycle=_NO_IDLE,
    )
    try:
        assert await r.ensure_ready() is ReadinessState.READY
        assert r.last_cause() == ""  # no failure -> no cause
    finally:
        await r.aclose()


async def test_default_controller_is_env_driven(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUSIC3_LIFECYCLE_ENABLED", raising=False)
    disabled = Readiness(_probe([ProbeOutcome.MODELS_AVAILABLE]))  # sentinel default, flag unset
    assert disabled.controller is None  # S2 behaviour preserved without touching main.py

    monkeypatch.setenv("MUSIC3_LIFECYCLE_ENABLED", "1")
    enabled = Readiness(_probe([ProbeOutcome.MODELS_AVAILABLE]))
    try:
        assert enabled.controller is not None  # real controller wired from env in production
    finally:
        await enabled.aclose()
