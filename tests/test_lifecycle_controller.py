"""Fixed-verb controller tests (spec: lifecycle-controller.md). CPU-only; httpx MockTransport, no docker.

Every INV-5 guarantee is proven against the module here, not by reading it: the closed verb set, the
absence of any container-name parameter, no shell/no input interpolation, and explicit surfacing of a
missing/denied/unresponsive socket (adversarial cases 1, 7, 8).
"""

import ast
import inspect
import logging

import httpx
import pytest

import lifecycle.controller as controller_module
from lifecycle.controller import (
    CONTAINER_NAME,
    ContainerState,
    ControllerError,
    ControllerUnavailable,
    DockerController,
    controller_from_env,
    lifecycle_config_from_env,
    parse_container_state,
)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ── Pure state mapping ──────────────────────────────────────────────────────────────────────────


def test_parse_container_state_running_stopped_is_pure() -> None:
    assert parse_container_state({"State": {"Running": True, "Status": "running"}}) is ContainerState.RUNNING
    assert parse_container_state({"State": {"Running": False, "Status": "exited"}}) is ContainerState.STOPPED
    assert parse_container_state({}) is ContainerState.STOPPED  # missing State -> not running


# ── status() over the Docker inspect endpoint ───────────────────────────────────────────────────


async def test_status_maps_running_stopped_absent() -> None:
    def running(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/containers/{CONTAINER_NAME}/json"  # the constant, nothing else
        return httpx.Response(200, json={"State": {"Running": True}})

    def stopped(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"State": {"Running": False}})

    def absent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "No such container"})

    assert await DockerController(transport=_transport(running)).status() is ContainerState.RUNNING
    assert await DockerController(transport=_transport(stopped)).status() is ContainerState.STOPPED
    assert await DockerController(transport=_transport(absent)).status() is ContainerState.ABSENT


async def test_status_server_error_is_unavailable_not_a_state() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "server error"})

    with pytest.raises(ControllerUnavailable):
        await DockerController(transport=_transport(boom)).status()


async def test_status_non_json_2xx_is_unavailable_not_a_crash() -> None:
    # A 2xx with an unparseable body must surface explicitly, never crash with a bare ValueError
    # (acceptance: a socket/daemon anomaly is never swallowed or fabricated into a state).
    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<not json>")

    with pytest.raises(ControllerUnavailable):
        await DockerController(transport=_transport(garbage)).status()


# ── start() / stop() verbs ──────────────────────────────────────────────────────────────────────


async def test_start_204_and_304_are_success() -> None:
    for code in (204, 304):
        def handler(request: httpx.Request, code=code) -> httpx.Response:
            assert request.url.path == f"/containers/{CONTAINER_NAME}/start"
            assert request.method == "POST"
            return httpx.Response(code)

        await DockerController(transport=_transport(handler)).start()  # no raise


async def test_start_absent_refuses_and_never_creates() -> None:
    def absent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "No such container"})

    with pytest.raises(ControllerError) as exc:
        await DockerController(transport=_transport(absent)).start()
    assert not isinstance(exc.value, ControllerUnavailable)  # a definitional refusal, not a socket failure


async def test_start_server_error_is_unavailable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(ControllerUnavailable):
        await DockerController(transport=_transport(boom)).start()


# ── SE-1: R-10 audit trail — every start/stop logs actor/verb/container/outcome ──────────────────


async def test_start_and_stop_emit_structured_audit_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="lifecycle.controller")

    def started(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    await DockerController(transport=_transport(started)).start()
    await DockerController(transport=_transport(started)).stop()
    text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "verb=start" in text and "verb=stop" in text
    assert f"container={CONTAINER_NAME}" in text
    assert "outcome=" in text  # the R-10 gate-briefing control (actor/verb/container/outcome) is real


async def test_failed_action_is_audited_with_error_outcome(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="lifecycle.controller")

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(ControllerUnavailable):
        await DockerController(transport=_transport(boom)).stop()
    text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "verb=stop" in text and "outcome=error" in text


async def test_stop_204_304_404_all_free_the_container() -> None:
    for code in (204, 304, 404):
        def handler(request: httpx.Request, code=code) -> httpx.Response:
            assert request.url.path == f"/containers/{CONTAINER_NAME}/stop"
            return httpx.Response(code)

        await DockerController(transport=_transport(handler)).stop()  # no raise (404 == already gone)


async def test_stop_server_error_is_unavailable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(ControllerUnavailable):
        await DockerController(transport=_transport(boom)).stop()


# ── Socket missing / permission-denied / unresponsive → explicit, never swallowed (adv. case 7) ──


async def test_socket_failure_surfaces_from_every_verb() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")  # missing/denied socket presents this way

    controller = DockerController(transport=_transport(refuse))
    with pytest.raises(ControllerUnavailable):
        await controller.status()
    with pytest.raises(ControllerUnavailable):
        await controller.start()
    with pytest.raises(ControllerUnavailable):
        await controller.stop()


# ── INV-5 static guarantees (adversarial cases 1, 8) ─────────────────────────────────────────────


def test_public_verbs_take_no_container_name_parameter() -> None:
    for verb in ("status", "start", "stop"):
        params = list(inspect.signature(getattr(DockerController, verb)).parameters)
        assert params == ["self"], f"{verb} must take no name parameter, got {params}"


def test_no_unlisted_verb_exists_to_call() -> None:
    for forbidden in ("exec", "run", "pull", "create", "rm", "remove", "commit", "kill", "restart", "build"):
        assert not hasattr(DockerController, forbidden), f"{forbidden} must not exist on the controller"


def test_module_imports_no_shell_or_subprocess_facility() -> None:
    # AST, not a raw-text grep: the module docstring legitimately mentions "no subprocess/shell" to
    # DOCUMENT their absence, so a substring grep would false-positive on the prose. The real guarantee is
    # that no process/shell module is imported and no such call exists.
    tree = ast.parse(inspect.getsource(controller_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"subprocess", "os", "pty", "commands", "sh"}), imported
    # No attribute call into a shell facility either (belt-and-suspenders over the import check).
    attr_calls = {
        f"{getattr(node.func.value, 'id', '')}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
    }
    assert attr_calls.isdisjoint({"os.system", "os.popen", "subprocess.run", "subprocess.Popen"})


def test_name_is_module_constant_and_no_health_route() -> None:
    assert CONTAINER_NAME == "minimax-music3-sglang"
    assert "/health" not in inspect.getsource(controller_module)  # never the generation-triggering route


# ── Env-driven lifecycle config (pure) ───────────────────────────────────────────────────────────


def test_lifecycle_config_disabled_when_flag_absent_or_false() -> None:
    assert lifecycle_config_from_env({}) is None
    assert lifecycle_config_from_env({"MUSIC3_LIFECYCLE_ENABLED": "0"}) is None
    assert lifecycle_config_from_env({"MUSIC3_LIFECYCLE_ENABLED": "false"}) is None


def test_lifecycle_config_enabled_with_s1_seeded_defaults() -> None:
    config = lifecycle_config_from_env({"MUSIC3_LIFECYCLE_ENABLED": "1"})
    assert config is not None
    assert config.readiness_timeout_seconds == 180.0  # S1 cold start 87 s + margin
    assert config.idle_timeout_seconds == 600.0
    assert config.poll_seconds == 3.0
    assert config.socket == "/var/run/docker.sock"


def test_lifecycle_config_overrides_parsed() -> None:
    config = lifecycle_config_from_env(
        {
            "MUSIC3_LIFECYCLE_ENABLED": "yes",
            "MUSIC3_READINESS_TIMEOUT_SECONDS": "240",
            "MUSIC3_IDLE_TIMEOUT_SECONDS": "300",
            "MUSIC3_READINESS_POLL_SECONDS": "5",
            "MUSIC3_DOCKER_SOCKET": "/tmp/docker.sock",
        }
    )
    assert config is not None
    assert (config.readiness_timeout_seconds, config.idle_timeout_seconds, config.poll_seconds) == (240.0, 300.0, 5.0)
    assert config.socket == "/tmp/docker.sock"


def test_controller_from_env_none_when_disabled_else_pair() -> None:
    assert controller_from_env({}) is None
    built = controller_from_env({"MUSIC3_LIFECYCLE_ENABLED": "1"})
    assert built is not None
    made, config = built
    assert isinstance(made, DockerController)
    assert config.readiness_timeout_seconds == 180.0


# ── CR-2: reject duration settings that would remove the bound or spin the poller ────────────────


@pytest.mark.parametrize("var", ["MUSIC3_READINESS_TIMEOUT_SECONDS", "MUSIC3_READINESS_POLL_SECONDS"])
@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "-1", "-0.5", "notanumber"])
def test_lifecycle_config_rejects_non_positive_or_non_finite_durations(var: str, bad: str) -> None:
    # inf removes the deadline bound; 0/negative spin the poller; nan breaks the sleep. Reject at parse.
    with pytest.raises(ValueError):
        lifecycle_config_from_env({"MUSIC3_LIFECYCLE_ENABLED": "1", var: bad})


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-1"])
def test_lifecycle_config_rejects_bad_idle_timeout(bad: str) -> None:
    with pytest.raises(ValueError):
        lifecycle_config_from_env({"MUSIC3_LIFECYCLE_ENABLED": "1", "MUSIC3_IDLE_TIMEOUT_SECONDS": bad})


def test_lifecycle_config_idle_zero_is_allowed_and_disables_idle_down() -> None:
    config = lifecycle_config_from_env({"MUSIC3_LIFECYCLE_ENABLED": "1", "MUSIC3_IDLE_TIMEOUT_SECONDS": "0"})
    assert config is not None
    assert config.idle_timeout_seconds == 0.0  # 0 disables idle-down (Readiness._arm_idle guards > 0)
