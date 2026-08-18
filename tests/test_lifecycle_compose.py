"""Compose extension tests (spec: compose-app-service.md).

Renders ``docker compose -f deploy/docker-compose.yml config`` and asserts the app service carries the
docker.sock mount and the lifecycle variables, the socket is not on the GPU service, and there is no
``service_healthy`` dependency on the GPU service (INV-6). Skips cleanly where docker is unavailable.
"""

import shutil
import subprocess

import pytest
import yaml

_COMPOSE = "deploy/docker-compose.yml"


def _config(path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", path, "config"],
        capture_output=True,
        text=True,
        check=False,
    )


def _skip_unless_compose_available() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    if subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, check=False).returncode != 0:
        pytest.skip("docker compose plugin not available")


def _render() -> dict:
    # TS-2: skip ONLY when docker/compose is unavailable. When it IS available, a non-zero `config` means a
    # broken compose file — FAIL (with stderr), never skip, or a broken file would silently preserve
    # confidence in the INV-6 checks (deterministic check 6 requires a successful render).
    _skip_unless_compose_available()
    proc = _config(_COMPOSE)
    if proc.returncode != 0:
        pytest.fail(f"docker compose config failed (exit {proc.returncode}): {proc.stderr[:500]}")
    return yaml.safe_load(proc.stdout)


def _volume_targets(service: dict) -> list[str]:
    out: list[str] = []
    for vol in service.get("volumes", []) or []:
        if isinstance(vol, str):
            out.append(vol)
        elif isinstance(vol, dict):
            out.append(f"{vol.get('source', '')}:{vol.get('target', '')}")
    return out


def _env_keys(service: dict) -> set[str]:
    env = service.get("environment", {}) or {}
    if isinstance(env, dict):
        return set(env)
    return {item.split("=", 1)[0] for item in env}


def _has_service_healthy_dep(config: dict) -> bool:
    for service in config.get("services", {}).values():
        deps = service.get("depends_on", {})
        if isinstance(deps, dict):
            for dep in deps.values():
                if isinstance(dep, dict) and "healthy" in str(dep.get("condition", "")):
                    return True
    return False


def test_app_service_has_socket_mount_and_lifecycle_vars() -> None:
    config = _render()
    services = config["services"]
    assert "app" in services, "compose must define the app service (S5 extension)"
    app = services["app"]
    assert any("/var/run/docker.sock" in target for target in _volume_targets(app)), "app needs the socket mount"
    keys = _env_keys(app)
    for required in (
        "MUSIC3_LIFECYCLE_ENABLED",
        "MUSIC3_READINESS_TIMEOUT_SECONDS",
        "MUSIC3_IDLE_TIMEOUT_SECONDS",
        "MUSIC3_READINESS_POLL_SECONDS",
    ):
        assert required in keys, f"app service must carry {required}"


def test_socket_is_not_mounted_into_the_gpu_service() -> None:
    config = _render()
    sglang = config["services"]["sglang"]
    assert not any("docker.sock" in target for target in _volume_targets(sglang)), "socket must be app-only (R-10)"


def test_no_service_healthy_dependency_on_gpu_service() -> None:
    config = _render()
    assert _has_service_healthy_dep(config) is False  # INV-6


def test_the_check_catches_an_injected_health_dependency() -> None:
    # Prove the predicate is real (adversarial case 9): a scratch config that adds the condition is caught.
    scratch = {
        "services": {
            "app": {"depends_on": {"sglang": {"condition": "service_healthy"}}},
            "sglang": {},
        }
    }
    assert _has_service_healthy_dep(scratch) is True
    assert _has_service_healthy_dep(_render()) is False


def test_broken_compose_config_exits_nonzero_so_render_would_fail_not_skip(tmp_path) -> None:
    # TS-2: prove the fail-path signal is real — an invalid compose file makes `docker compose config` exit
    # non-zero, so _render's pytest.fail branch (not its skip branch) would fire on a broken deploy file.
    _skip_unless_compose_available()
    broken = tmp_path / "broken.yml"
    broken.write_text("services:\n  app:\n    image:\n      - not: a valid image spec\n")
    proc = _config(str(broken))
    assert proc.returncode != 0  # docker rejects it -> _render would FAIL, never silently skip
