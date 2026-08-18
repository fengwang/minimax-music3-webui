"""Idle-down + lease tests (spec: idle-down-lease.md). CPU-only; fake controller + fake clock.

The safety property — idle-down never fires while a lease is held or before the idle interval elapses
(adversarial case 3) — is proven against the pure predicate and the ``_maybe_idle_down`` action directly,
with no reliance on the background loop's real-time timing.
"""

import asyncio

import pytest

from fakes import FakeController
from lifecycle.controller import ContainerState, LifecycleConfig
from lifecycle.readiness import ProbeOutcome, Readiness, ReadinessState, _next_idle_sleep, should_idle_down


async def _ready_probe() -> ProbeOutcome:
    return ProbeOutcome.MODELS_AVAILABLE


def _readiness(controller: FakeController, clock, idle_interval: float = 600.0) -> Readiness:
    return Readiness(
        _ready_probe,
        controller=controller,
        lifecycle=LifecycleConfig(socket="x", readiness_timeout_seconds=1.0, idle_timeout_seconds=idle_interval, poll_seconds=1.0),
        clock=clock,
    )


# ── The pure decision (adversarial case 3 in truth-table form) ───────────────────────────────────


def test_should_idle_down_truth_table() -> None:
    assert should_idle_down(leases=0, idle_for=700, idle_interval=600, running=True) is True
    assert should_idle_down(leases=1, idle_for=700, idle_interval=600, running=True) is False  # lease held
    assert should_idle_down(leases=2, idle_for=99999, idle_interval=600, running=True) is False  # any lease
    assert should_idle_down(leases=0, idle_for=500, idle_interval=600, running=True) is False  # too soon
    assert should_idle_down(leases=0, idle_for=700, idle_interval=600, running=False) is False  # not up


# ── The action, driven deterministically ─────────────────────────────────────────────────────────


async def test_idle_down_stops_when_idle_and_running() -> None:
    t = [0.0]
    controller = FakeController([ContainerState.RUNNING])
    r = _readiness(controller, lambda: t[0])
    try:
        t[0] = 1000.0  # idle_for = 1000 - 0 (init) >= 600
        await r._maybe_idle_down()
        assert controller.stop_calls == 1
    finally:
        await r.aclose()


async def test_lease_held_blocks_idle_down_then_releases() -> None:
    t = [0.0]
    controller = FakeController([ContainerState.RUNNING])
    r = _readiness(controller, lambda: t[0])
    try:
        t[0] = 1000.0
        async with r.lease():
            await r._maybe_idle_down()
            assert controller.stop_calls == 0  # a held lease blocks idle-down for the whole generation
        # lease exit stamped _last_activity = 1000, so it is not yet idle again
        await r._maybe_idle_down()
        assert controller.stop_calls == 0
        t[0] = 2000.0  # now idle for 1000 s since the lease released
        await r._maybe_idle_down()
        assert controller.stop_calls == 1
    finally:
        await r.aclose()


async def test_recent_activity_blocks_idle_down() -> None:
    t = [0.0]
    controller = FakeController([ContainerState.RUNNING])
    r = _readiness(controller, lambda: t[0])
    try:
        t[0] = 100.0  # idle_for = 100 < 600
        await r._maybe_idle_down()
        assert controller.stop_calls == 0
    finally:
        await r.aclose()


async def test_idle_down_invalidates_ready_cache() -> None:
    t = [0.0]
    controller = FakeController([ContainerState.RUNNING])
    r = _readiness(controller, lambda: t[0])
    try:
        assert await r.ensure_ready() is ReadinessState.READY  # caches READY
        assert r.last_known() is ReadinessState.READY
        t[0] = 1000.0
        await r._maybe_idle_down()
        assert controller.stop_calls == 1
        assert r.last_known() is ReadinessState.WARMING  # cache invalidated -> next call cold-starts
    finally:
        await r.aclose()


async def test_idle_loop_self_arms_on_first_ensure_ready() -> None:
    controller = FakeController([ContainerState.RUNNING])
    r = _readiness(controller, lambda: 0.0)
    try:
        assert r._idle_task is None
        await r.ensure_ready()
        assert r._idle_task is not None  # main.py cannot start it; first readiness activity arms it
    finally:
        await r.aclose()


async def test_lease_released_on_exception() -> None:
    r = Readiness(_ready_probe, controller=None)  # no controller -> no idle loop; lease still works
    with pytest.raises(RuntimeError):
        async with r.lease():
            assert r._leases == 1
            raise RuntimeError("boom")
    assert r._leases == 0  # released despite the exception (finally in the context manager)


def test_next_idle_sleep_never_busy_spins() -> None:
    # A long leased generation drives idle_for far past the interval; the sleep must clamp to poll_seconds
    # rather than collapse toward zero (the wake-storm the perf review caught).
    assert _next_idle_sleep(idle_for=5000, idle_interval=600, poll_seconds=3) == 3
    assert _next_idle_sleep(idle_for=600, idle_interval=600, poll_seconds=3) == 3  # at expiry -> poll floor
    # Idle but not yet expired: sleep the remaining time so it wakes right at the deadline.
    assert _next_idle_sleep(idle_for=100, idle_interval=600, poll_seconds=3) == 500


async def test_ready_cache_invalidated_before_stop_await() -> None:
    # adversarial case 3 (misconfig variant): the READY cache MUST be cleared before stop() yields, so a
    # concurrent fast-path caller cannot lease onto a stopping engine. Uses a huge cache_seconds so a stale
    # READY would otherwise survive the stop window.
    t = [0.0]
    release = asyncio.Event()

    class _SlowStopController:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def status(self) -> ContainerState:
            return ContainerState.RUNNING

        async def start(self) -> None: ...

        async def stop(self) -> None:
            self.stop_calls += 1
            await release.wait()  # hold the stop mid-await

    controller = _SlowStopController()
    r = Readiness(
        _ready_probe,
        controller=controller,
        lifecycle=LifecycleConfig("x", 1.0, 600.0, 1.0),
        cache_seconds=10_000.0,  # misconfig: cache outlives the idle interval
        clock=lambda: t[0],
    )
    try:
        await r.ensure_ready()  # caches READY
        assert r._fresh_ready() is not None
        t[0] = 1000.0
        task = asyncio.create_task(r._maybe_idle_down())
        await asyncio.sleep(0.02)  # let it reach the stop() await
        assert controller.stop_calls == 1  # stopping now, mid-await
        assert r._fresh_ready() is None  # cache already invalidated BEFORE the stop await -> no fast-path
        release.set()
        await task
    finally:
        await r.aclose()


async def test_idle_down_blocked_while_lock_held() -> None:
    # adversarial case 3 (cold-start window): while ensure_ready holds the shared lock (a cold start in
    # flight), _maybe_idle_down must not stop the container until the lock frees. Deleting the `async with
    # self._lock` in _maybe_idle_down makes this fail.
    t = [0.0]
    controller = FakeController([ContainerState.RUNNING])
    r = _readiness(controller, lambda: t[0])
    try:
        t[0] = 1000.0
        async with r._lock:  # simulate an in-flight cold start holding the lock
            task = asyncio.create_task(r._maybe_idle_down())
            await asyncio.sleep(0.02)
            assert controller.stop_calls == 0  # blocked on the lock, cannot stop mid-cold-start
        await task  # lock released -> idle-down proceeds
        assert controller.stop_calls == 1
    finally:
        await r.aclose()
