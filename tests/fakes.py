"""Test doubles for the CPU-only suite. No network, no GPU, no engine.

``FakeEngineClient`` implements the ``EngineClient`` protocol in-process. It tracks concurrency
(``max_active`` proves INV-1), records whether its call was cancelled (proves cancel-no-orphan), counts
calls (proves a cancelled-while-queued job never reaches the engine), and can hang on a gate or raise on
demand.
"""

import asyncio
from contextlib import asynccontextmanager

from engines.sglang.client import EngineError, EngineRequest, EngineResult
from lifecycle.controller import ContainerState, ControllerError
from lifecycle.readiness import ReadinessState


class FakeReadiness:
    """An in-process readiness gate for tests. Returns a fixed state and counts ``ensure_ready`` calls.

    ``lease()`` (A-S5-01) tracks concurrent leases so a test can prove the runner holds one across the whole
    engine call and releases it on success, failure and cancel alike.
    """

    def __init__(self, state: ReadinessState = ReadinessState.READY, cause: str = "") -> None:
        self.state = state
        self.cause = cause
        self.calls = 0
        self.leases = 0
        self.max_leases = 0

    async def ensure_ready(self) -> ReadinessState:
        self.calls += 1
        return self.state

    @asynccontextmanager
    async def lease(self):
        self.leases += 1
        self.max_leases = max(self.max_leases, self.leases)
        try:
            yield
        finally:
            self.leases -= 1

    def last_known(self) -> ReadinessState:
        return self.state

    def last_cause(self) -> str:
        return self.cause

    def start_idle_monitor(self) -> None:
        pass  # no idle loop in the fake; the lifespan (A-S5-02) calls this on any injected readiness

    async def aclose(self) -> None:
        pass


class FakeController:
    """In-process container-controller double (implements the controller surface readiness depends on).

    Scripts ``status()`` returns (consumed one-per-call, last repeats) and counts ``start``/``stop``. When
    ``fail`` is set, every verb raises it — modelling a missing/denied/unresponsive socket.
    """

    def __init__(self, status_states: list[ContainerState] | None = None, *, fail: ControllerError | None = None) -> None:
        self._states = list(status_states or [ContainerState.RUNNING])
        self._fail = fail
        self.start_calls = 0
        self.stop_calls = 0

    async def status(self) -> ContainerState:
        if self._fail is not None:
            raise self._fail
        return self._states.pop(0) if len(self._states) > 1 else self._states[0]

    async def start(self) -> None:
        if self._fail is not None:
            raise self._fail
        self.start_calls += 1

    async def stop(self) -> None:
        if self._fail is not None:
            raise self._fail
        self.stop_calls += 1


class FakeEngineClient:
    """An in-process engine double.

    Args:
        default: behaviour when the per-call list is exhausted — ``"succeed"``, ``"hang"``, or ``"raise"``.
        behaviors: optional list consumed one-per-call, overriding ``default`` for those calls.
        delay: optional sleep (seconds) before a successful return.
    """

    def __init__(self, *, default: str = "succeed", behaviors: list[str] | None = None, delay: float = 0.0) -> None:
        self.default = default
        self._behaviors = list(behaviors or [])
        self._delay = delay
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.cancelled = False
        self._gate = asyncio.Event()

    def release(self) -> None:
        """Release every call currently hanging (and any future hanging call)."""
        self._gate.set()

    def _next_behavior(self) -> str:
        return self._behaviors.pop(0) if self._behaviors else self.default

    async def generate(self, request: EngineRequest) -> EngineResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            behavior = self._next_behavior()
            if behavior == "raise":
                raise EngineError("fake engine failure")
            if behavior == "hang":
                await self._gate.wait()
            elif self._delay:
                await asyncio.sleep(self._delay)
            return EngineResult(
                audio=b"RIFFwav", content_type="audio/wav",
                engine="fake", model=request.model, generation_seconds=0.01,
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.active -= 1
