"""Shared pytest fixtures.

Hermeticity for S5's env-driven lifecycle wiring (A-S5-01): ``Readiness(probe)`` — the construction path the
frozen ``api/app/main.py`` uses — reads ``MUSIC3_LIFECYCLE_ENABLED`` from the process environment to decide
whether to build a real docker.sock controller. If that variable leaked into the pytest process, the S2
tests that build ``Readiness(probe)`` (``test_app.py``, ``test_readiness.py``) would try to reach the real
socket on the first job and could hang (~180 s readiness budget) or start the GPU container. This autouse
fixture strips the lifecycle variables so the CPU suite is hermetic regardless of the ambient environment.
Tests that exercise lifecycle inject a controller + config explicitly and are unaffected.
"""

import pytest

_LIFECYCLE_ENV = (
    "MUSIC3_LIFECYCLE_ENABLED",
    "MUSIC3_DOCKER_SOCKET",
    "MUSIC3_READINESS_TIMEOUT_SECONDS",
    "MUSIC3_IDLE_TIMEOUT_SECONDS",
    "MUSIC3_READINESS_POLL_SECONDS",
)


@pytest.fixture(autouse=True)
def _isolate_lifecycle_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete the S5 lifecycle env vars before every test so `Readiness(probe)` stays probe-only (S2)."""
    for name in _LIFECYCLE_ENV:
        monkeypatch.delenv(name, raising=False)
