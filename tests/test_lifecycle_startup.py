"""App-startup lifecycle tests (codex CR-1 / A-S5-02). CPU-only; fake controller, no GPU.

An initially-idle deployment (``docker compose up`` starts the GPU container) must idle the container down
even if no job is ever submitted. That requires the app lifespan to arm the idle monitor at startup — not
lazily on the first ``ensure_ready()``. This drives the real lifespan via TestClient and asserts the
monitor is armed with no job, and torn down on shutdown.
"""

from fastapi.testclient import TestClient

from app.main import config_from_env, create_app
from fakes import FakeController, FakeEngineClient
from lifecycle.controller import ContainerState, LifecycleConfig
from lifecycle.readiness import ProbeOutcome, Readiness

_LC = LifecycleConfig(socket="x", readiness_timeout_seconds=0.5, idle_timeout_seconds=3600.0, poll_seconds=0.01)


async def _ready_probe() -> ProbeOutcome:
    return ProbeOutcome.MODELS_AVAILABLE


def test_idle_monitor_arms_at_app_startup_without_a_job() -> None:
    controller = FakeController([ContainerState.RUNNING])
    readiness = Readiness(_ready_probe, controller=controller, lifecycle=_LC)
    app = create_app(config=config_from_env({}), engine=FakeEngineClient(), readiness=readiness)

    assert readiness._idle_task is None  # not armed before startup
    with TestClient(app):  # runs the lifespan startup — NO job submitted
        assert readiness._idle_task is not None  # A-S5-02: armed at boot so idle-down can free VRAM
    assert readiness._idle_task is None  # lifespan shutdown called aclose() and tore it down


def test_startup_without_a_controller_is_a_noop() -> None:
    # The CPU/default deployment (lifecycle disabled) arms nothing — start_idle_monitor is a safe no-op.
    readiness = Readiness(_ready_probe, controller=None)
    app = create_app(config=config_from_env({}), engine=FakeEngineClient(), readiness=readiness)
    with TestClient(app):
        assert readiness._idle_task is None
