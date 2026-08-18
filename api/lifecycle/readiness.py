"""The single readiness decision for the whole app: ``ensure_ready()``.

Readiness means "the engine is loaded and able to accept one request". SGLang-Omni's implementation of
that is a successful ``GET /v1/models``; the engine's own generation-triggering route is never polled
(E-15, R-16), and INV-6 pins ``/v1/models`` regardless.

S5 fills this seam in without changing its signature or its three states. Behind ``ensure_ready()`` it now
drives the container lifecycle when a controller is configured: it brings a stopped engine up on demand,
coalesces concurrent callers onto exactly one cold start, waits a bounded readiness budget, and idles the
engine down after a configured idle interval — but only while no in-flight request holds a lease. All
container mechanics live in ``lifecycle.controller``; this module depends on that abstract surface only, so
the readiness contract names no container runtime and a fallback engine could supply a different controller.

ACD: ``classify_readiness`` and ``should_idle_down`` are pure Calculations; the probe, ``ensure_ready()``,
``lease()`` and the idle loop are the Action shell. Concurrent callers collapse onto one probe/start via a
single lock plus a short last-known READY cache; the idle loop re-validates its decision inside that same
lock, so a stop can never interleave with a probe or a cold start.
"""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from enum import Enum

import httpx

from lifecycle.controller import (
    ContainerState,
    ControllerError,
    LifecycleConfig,
    controller_from_env,
)

_log = logging.getLogger(__name__)

#: The only readiness signal (INV-6, E-14). This path appears nowhere else in the codebase.
MODELS_PATH = "/v1/models"

# Fallback lifecycle timings when a controller is injected without an explicit config (tests). Production
# gets these from ``LifecycleConfig`` (S1-seeded: cold start 87 s -> 180 s readiness budget; idle 600 s).
_DEFAULT_READINESS_TIMEOUT = 180.0
_DEFAULT_IDLE_INTERVAL = 600.0
_DEFAULT_POLL_SECONDS = 3.0

#: Sentinel: "build the controller from the environment" — the path ``main.py`` takes, since it is frozen
#: and passes neither a controller nor a lifecycle config.
_FROM_ENV = object()


class ReadinessState(str, Enum):
    """The three states ``ensure_ready()`` reports."""

    READY = "ready"
    WARMING = "warming"
    UNAVAILABLE = "unavailable"


class ProbeOutcome(str, Enum):
    """The result of a single readiness probe."""

    MODELS_AVAILABLE = "models_available"
    LOADING = "loading"
    UNREACHABLE = "unreachable"


Probe = Callable[[], Awaitable[ProbeOutcome]]


def classify_readiness(outcome: ProbeOutcome) -> ReadinessState:
    """Map a single probe outcome to a readiness state. Pure."""
    if outcome is ProbeOutcome.MODELS_AVAILABLE:
        return ReadinessState.READY
    if outcome is ProbeOutcome.LOADING:
        return ReadinessState.WARMING
    return ReadinessState.UNAVAILABLE


def should_idle_down(leases: int, idle_for: float, idle_interval: float, running: bool) -> bool:
    """Whether the engine may be idled down now. Pure.

    True only when nothing holds the slot (``leases == 0``), the engine has been idle at least
    ``idle_interval``, and it is actually running. Driven by slot state, never by wall clock alone, so it
    can never fire during an in-flight generation.
    """
    return leases == 0 and idle_for >= idle_interval and running


def _next_idle_sleep(idle_for: float, idle_interval: float, poll_seconds: float) -> float:
    """Seconds to sleep before the next idle re-check. Pure.

    At least ``poll_seconds`` so a long generation — during which a lease is held and ``idle_for`` can grow
    past ``idle_interval`` — does not busy-spin the idle loop; otherwise sleep until the interval could just
    elapse. Clamped to ``poll_seconds`` at the low end, so it never collapses toward zero.
    """
    return max(poll_seconds, idle_interval - idle_for)


def make_models_probe(
    base_url: str,
    timeout: float,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Probe:
    """Build the real readiness probe: one bounded ``GET /v1/models``.

    2xx -> ``MODELS_AVAILABLE``; any other reachable response -> ``LOADING`` (engine up but not serving
    yet); a transport failure or timeout -> ``UNREACHABLE``. Never blocks beyond ``timeout`` — this bounded
    per-probe timeout is the budget that keeps ``ensure_ready()`` from hanging.
    """
    base = base_url.rstrip("/")

    async def probe() -> ProbeOutcome:
        try:
            async with httpx.AsyncClient(base_url=base, timeout=timeout, transport=transport) as client:
                response = await client.get(MODELS_PATH)
        except httpx.HTTPError:
            return ProbeOutcome.UNREACHABLE
        if response.status_code // 100 == 2:
            return ProbeOutcome.MODELS_AVAILABLE
        return ProbeOutcome.LOADING

    return probe


class Readiness:
    """Single-flight, cache-backed readiness with on-demand cold start and idle-down.

    ``ensure_ready()`` runs at most one probe/start for concurrent callers and reuses a recent READY result
    within ``cache_seconds``, so the engine is never stormed. When a controller is configured it will start
    a stopped engine and wait a bounded readiness budget; otherwise it reports the probe state only (the S2
    contract, preserved exactly).

    Args:
        probe: async callable returning a ``ProbeOutcome`` (the real one from ``make_models_probe`` or a
            fake in tests).
        cache_seconds: how long a READY probe result is treated as last-known.
        clock: monotonic clock, injectable for tests.
        controller: the container controller, or ``None`` for probe-only (S2) behaviour. Left as the
            ``_FROM_ENV`` sentinel — the path ``main.py`` takes — it is built from the environment, enabled
            only when ``MUSIC3_LIFECYCLE_ENABLED`` is set (so the CPU suite stays probe-only).
        lifecycle: the timeouts; defaults are used when a controller is injected without one.
    """

    def __init__(
        self,
        probe: Probe,
        *,
        cache_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        controller: object = _FROM_ENV,
        lifecycle: LifecycleConfig | None = None,
    ) -> None:
        self._probe = probe
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cached: tuple[ReadinessState, float] | None = None
        # Lifecycle wiring. main.py (frozen) constructs Readiness(probe, cache_seconds=X) with neither a
        # controller nor a lifecycle config, so the sentinel triggers the single os.environ read that builds
        # a real controller iff MUSIC3_LIFECYCLE_ENABLED is set. Tests inject a controller + lifecycle, or
        # pass controller=None for probe-only behaviour.
        if controller is _FROM_ENV:
            built = controller_from_env(os.environ)  # the one Action/impurity, guarded to this path
            controller, lifecycle = built if built is not None else (None, None)
        self._controller = controller
        self._readiness_timeout = lifecycle.readiness_timeout_seconds if lifecycle else _DEFAULT_READINESS_TIMEOUT
        self._idle_interval = lifecycle.idle_timeout_seconds if lifecycle else _DEFAULT_IDLE_INTERVAL
        self._poll_seconds = lifecycle.poll_seconds if lifecycle else _DEFAULT_POLL_SECONDS
        self._leases = 0
        self._last_activity = clock()
        self._idle_task: asyncio.Task | None = None
        self._last_cause = ""  # AR-1: sanitized cause of the last non-READY result, surfaceable to callers

    @property
    def controller(self) -> object | None:
        """The configured container controller, or ``None`` when lifecycle is off (introspection/tests)."""
        return self._controller

    def _fresh_ready(self) -> ReadinessState | None:
        """Return cached READY if still within the cache window, else ``None``.

        Only READY is cached: a WARMING/UNAVAILABLE result must never short-circuit a cold start, or a job
        would be failed as not-ready while the engine is coming up.
        """
        cached = self._cached
        if cached is not None and cached[0] is ReadinessState.READY and (self._clock() - cached[1]) < self._cache_seconds:
            return cached[0]
        return None

    async def ensure_ready(self) -> ReadinessState:
        """Return the current readiness state, driving one cold start for concurrent callers when needed."""
        self._last_activity = self._clock()  # any readiness activity restarts the idle timer
        self._arm_idle()
        fresh = self._fresh_ready()
        if fresh is not None:
            return fresh
        async with self._lock:
            fresh = self._fresh_ready()  # another caller may have brought it up while we waited
            if fresh is not None:
                return fresh
            state = await self._drive_readiness()
            if state is ReadinessState.READY:
                now = self._clock()
                self._cached = (state, now)
                self._last_activity = now  # ready now; restart the idle timer from the cold-start completion
                self._last_cause = ""  # AR-1: no failure -> no cause
            return state

    async def _drive_readiness(self) -> ReadinessState:
        """Probe once; if not ready and a controller is configured, start the engine and poll to READY.

        CR-3: the readiness deadline is established BEFORE the initial probe, so the whole cold path
        (initial probe + status + start + poll) is bounded by ``readiness_timeout`` rather than only the
        poll loop. status/start are each bounded by the controller's own HTTP timeout and abort on failure,
        so they cannot extend the wait past that bound.
        """
        deadline = self._clock() + self._readiness_timeout
        state = classify_readiness(await self._probe())
        if state is ReadinessState.READY or self._controller is None:
            return state  # already serving, or S2 probe-only mode
        try:
            container = await self._controller.status()
            if container is ContainerState.ABSENT:
                self._last_cause = "engine container is absent (not defined by compose); cannot start"
                _log.warning(self._last_cause)
                return ReadinessState.UNAVAILABLE
            if container is ContainerState.STOPPED:
                _log.warning("engine container is stopped; issuing one start")
                await self._controller.start()
        except ControllerError as exc:  # missing/denied/unresponsive socket, or a definitional refusal
            self._last_cause = f"lifecycle controller unavailable: {exc}"
            _log.warning(self._last_cause)
            return ReadinessState.UNAVAILABLE
        return await self._poll_until_ready(deadline)

    async def _poll_until_ready(self, deadline: float) -> ReadinessState:
        """Poll ``/v1/models`` until READY or the ``deadline``; return the last non-READY state then, with a
        surfaceable cause recorded (AR-1)."""
        state = ReadinessState.WARMING
        while True:
            state = classify_readiness(await self._probe())
            if state is ReadinessState.READY:
                return state
            if self._clock() >= deadline:
                self._last_cause = (
                    f"engine did not become ready within {self._readiness_timeout:.0f}s "
                    f"(last state={state.value})"
                )
                _log.warning(self._last_cause)
                return state
            await asyncio.sleep(self._poll_seconds)

    @asynccontextmanager
    async def lease(self):
        """Held by the in-flight request for the duration of a generation (A-S5-01).

        While any lease is held the idle loop cannot stop the engine. Enter/exit perform no ``await``, so
        the lease introduces no new cancel/interleave race, and it is released on success, engine failure
        and cancellation alike.
        """
        self._leases += 1
        try:
            yield
        finally:
            self._leases -= 1
            self._last_activity = self._clock()

    def start_idle_monitor(self) -> None:
        """Arm the idle-down loop from the app lifespan at startup (codex CR-1, A-S5-02).

        Compose starts the GPU container immediately at deploy time, so idle-down must run from boot — not
        only after the first job's ``ensure_ready()`` — or an initially-idle deployment would hold VRAM
        forever. Idempotent; a no-op when no controller is configured (the CPU suite) or idle is disabled.
        Must be called from within a running event loop (the lifespan provides one).
        """
        self._arm_idle()

    def _arm_idle(self) -> None:
        """Create the idle-down loop once (on app startup, or on the first readiness activity)."""
        if self._controller is not None and self._idle_task is None and self._idle_interval > 0:
            self._idle_task = asyncio.create_task(self._idle_loop())

    async def _idle_loop(self) -> None:
        """Wake periodically and idle the engine down once it has been unused for the idle interval.

        Cancellation (from ``aclose()`` or loop teardown) propagates out of the awaited sleep and unwinds
        the loop cleanly — no handler is needed.
        """
        while True:
            idle_for = self._clock() - self._last_activity
            if self._leases == 0 and idle_for >= self._idle_interval:
                await self._maybe_idle_down()
                await asyncio.sleep(self._idle_interval)
            else:
                await asyncio.sleep(_next_idle_sleep(idle_for, self._idle_interval, self._poll_seconds))

    async def _maybe_idle_down(self) -> None:
        """Stop the engine iff the idle predicate still holds — re-checked INSIDE the shared lock so a stop
        can never interleave with a probe or a cold start (adversarial case 3)."""
        async with self._lock:
            try:
                running = await self._controller.status() is ContainerState.RUNNING
            except ControllerError as exc:
                _log.warning("idle-down status check failed: %s", exc)
                return
            idle_for = self._clock() - self._last_activity
            if not should_idle_down(self._leases, idle_for, self._idle_interval, running):
                return
            _log.warning("idle-down: stopping engine after %.0fs idle to free VRAM", idle_for)
            # Invalidate the READY cache BEFORE the stop() await: otherwise a concurrent ensure_ready()
            # could take the lock-free fast path on a still-fresh cache and lease onto an engine that is
            # being stopped (adversarial case 3 under a cache_seconds >= idle_interval misconfig). With the
            # cache cleared first, such a caller finds no fresh READY and blocks on the shared lock instead.
            self._cached = None
            try:
                await self._controller.stop()
            except ControllerError as exc:
                _log.warning("idle-down stop failed: %s", exc)
                return

    async def aclose(self) -> None:
        """Cancel the idle loop and wait for it to unwind (test/shutdown cleanliness)."""
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None

    def last_cause(self) -> str:
        """The sanitized cause of the last non-READY result (empty once READY), surfaceable to callers.

        Lets a queued caller distinguish a timeout from an absent container from a control-socket denial
        without operator log access (AR-1). Carries no client-derived data — only lifecycle-internal
        reasons.
        """
        return self._last_cause

    def last_known(self) -> ReadinessState:
        """The last observed state without probing; ``WARMING`` until the first READY is cached."""
        cached = self._cached
        return cached[0] if cached is not None else ReadinessState.WARMING
