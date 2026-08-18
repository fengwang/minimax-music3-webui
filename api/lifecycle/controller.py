"""The fixed-verb GPU-container controller: status/start/stop for ONE named container over the Docker
Engine HTTP API on a bind-mounted ``/var/run/docker.sock`` (INV-5, R-10).

This is the ONLY module that knows the GPU container is managed via Docker. ``api/lifecycle/readiness.py``
depends on it through the abstract ``status()/start()/stop()`` surface plus a ``ContainerState``, so the
readiness contract names no container runtime (project_contract §4) and a fallback engine could supply a
different controller.

Security (INV-5, R-10 compensating controls):

- The container name is a **module-level constant** compared for exact equality. The public methods take
  **no name parameter**, so a name placed in a request field or header has nowhere to arrive.
- The verb set is **closed** — status, start, stop — and any other Docker operation (``exec``, ``run``,
  ``pull``, ``create``, ``rm``, image operations, arbitrary command strings) is **absent** rather than
  present-and-unused.
- **No shell, no subprocess**, and **no interpolation of client input** into any argument: the only value
  interpolated into a path is the module constant, and the verbs are fixed literal path segments of the
  HTTP API.

ACD: ``parse_container_state`` and ``lifecycle_config_from_env`` are pure Calculations; ``DockerController``
is the Action shell issuing one bounded HTTP request per verb. A missing / permission-denied / unresponsive
socket, or a Docker server error, surfaces as an explicit ``ControllerUnavailable`` — never a swallowed
success or a fabricated state (adversarial case 7).
"""

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

import httpx

_log = logging.getLogger(__name__)
#: The app service is the sole lifecycle actor (unauthenticated LAN, D5/R-10). No per-request actor exists.
_ACTOR = "app"

#: The one container this app controls (INV-5). Exact-equality target; NEVER taken from input.
CONTAINER_NAME = "minimax-music3-sglang"
#: The bind-mounted Docker Engine socket (R-10). Overridable by config; never client input.
DEFAULT_SOCKET = "/var/run/docker.sock"
#: Dummy authority for the UDS transport — httpx needs a host in the URL; the socket does the routing.
_BASE_URL = "http://docker"
#: Bounded HTTP timeout. > Docker's default 10 s stop grace so a slow stop is not mistaken for a hang.
_DEFAULT_HTTP_TIMEOUT = 30.0


class ContainerState(str, Enum):
    """The three container states the controller reports."""

    RUNNING = "running"
    STOPPED = "stopped"
    ABSENT = "absent"  # not defined by compose; the controller never creates it (INV-3/INV-4)


class ControllerError(Exception):
    """Base for lifecycle-controller failures."""


class ControllerUnavailable(ControllerError):
    """The docker socket is missing, permission-denied, unresponsive, or returned a server error.

    Surfaced explicitly, never swallowed into a false state, so the readiness seam can report UNAVAILABLE
    with a cause rather than hang or fake a down/ready state.
    """


def parse_container_state(inspect: Mapping[str, object]) -> ContainerState:
    """Map a Docker inspect body's ``State`` object to a ``ContainerState``. Pure.

    ``State.Running`` true -> RUNNING; any other reachable inspect -> STOPPED. ABSENT is a 404 handled by
    the caller, not a shape represented in an inspect body.
    """
    state = inspect.get("State")
    if isinstance(state, Mapping) and state.get("Running") is True:
        return ContainerState.RUNNING
    return ContainerState.STOPPED


def _audit(verb: str, outcome: str) -> None:
    """R-10 audit trail: one structured line per lifecycle action (actor/verb/container/outcome).

    The container is always the module constant and the actor is always the app service, so the line
    carries no client-derived data — it records that the app started/stopped the one fixed container and
    how it turned out, which is the compensating control presented at GATE-S5.
    """
    _log.info("lifecycle actor=%s verb=%s container=%s outcome=%s", _ACTOR, verb, CONTAINER_NAME, outcome)


@dataclass(frozen=True)
class LifecycleConfig:
    """Parsed lifecycle knobs. Present only when lifecycle is enabled. Immutable Data."""

    socket: str
    readiness_timeout_seconds: float
    idle_timeout_seconds: float
    poll_seconds: float


class DockerController:
    """status / start / stop for :data:`CONTAINER_NAME` over the Docker Engine HTTP API.

    No public method takes a container-name parameter. ``transport`` is injected in tests (an
    ``httpx.MockTransport``); in production it defaults to a UNIX-domain-socket transport onto the mounted
    docker socket, so no new dependency is added (``httpx`` is already used by the readiness probe).
    """

    def __init__(
        self,
        *,
        socket: str = DEFAULT_SOCKET,
        timeout: float = _DEFAULT_HTTP_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._socket = socket
        self._timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        transport = self._transport or httpx.AsyncHTTPTransport(uds=self._socket)
        return httpx.AsyncClient(base_url=_BASE_URL, timeout=self._timeout, transport=transport)

    async def status(self) -> ContainerState:
        """Report the container state via one bounded ``GET /containers/<name>/json``."""
        try:
            async with self._client() as client:
                response = await client.get(f"/containers/{CONTAINER_NAME}/json")
        except httpx.HTTPError as exc:
            raise ControllerUnavailable(f"docker inspect failed: {exc!r}") from exc
        if response.status_code == 404:
            return ContainerState.ABSENT
        if response.status_code // 100 != 2:
            raise ControllerUnavailable(f"docker inspect returned status {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:  # a 2xx with a non-JSON body is a daemon/proxy anomaly, not a state
            raise ControllerUnavailable(f"docker inspect returned a non-JSON body: {exc!r}") from exc
        return parse_container_state(body)

    async def start(self) -> None:
        """Start the container via one bounded ``POST /containers/<name>/start``.

        204 (started) and 304 (already started) are success. A 404 means compose never defined the
        container: the controller refuses rather than creating one (INV-3/INV-4 preserved negatively).
        Emits one R-10 audit line (actor/verb/container/outcome) on every path, success or failure.
        """
        outcome = "error"
        try:
            response = await self._post(f"/containers/{CONTAINER_NAME}/start")
            if response.status_code in (204, 304):
                outcome = "started" if response.status_code == 204 else "already_running"
                return
            if response.status_code == 404:
                raise ControllerError(f"container {CONTAINER_NAME!r} is not defined (404); refusing to create it")
            raise ControllerUnavailable(f"docker start returned status {response.status_code}")
        finally:
            _audit("start", outcome)

    async def stop(self) -> None:
        """Stop the container via one bounded ``POST /containers/<name>/stop``.

        204 (stopped), 304 (already stopped) and 404 (already gone -> VRAM already freed) are all success.
        Emits one R-10 audit line on every path.
        """
        outcome = "error"
        try:
            response = await self._post(f"/containers/{CONTAINER_NAME}/stop")
            if response.status_code in (204, 304, 404):
                outcome = {204: "stopped", 304: "already_stopped", 404: "absent"}[response.status_code]
                return
            raise ControllerUnavailable(f"docker stop returned status {response.status_code}")
        finally:
            _audit("stop", outcome)

    async def _post(self, path: str) -> httpx.Response:
        try:
            async with self._client() as client:
                return await client.post(path)
        except httpx.HTTPError as exc:
            raise ControllerUnavailable(f"docker POST {path} failed: {exc!r}") from exc


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _duration(env: Mapping[str, str], key: str, default: str, *, allow_zero: bool) -> float:
    """Parse a duration env var, rejecting anything that would break the lifecycle (CR-2). Pure.

    A non-finite value (``nan``/``inf``) would remove the readiness deadline bound; a zero or negative poll
    would spin the poller. So readiness timeout and poll must be positive-finite; the idle interval may be
    zero (which disables idle-down via ``Readiness._arm_idle``'s ``> 0`` guard) but never negative or
    non-finite. Raises ``ValueError`` at parse so a bad deployment setting fails loudly at startup.
    """
    raw = env.get(key, default)
    value = float(raw)  # a non-numeric string already raises ValueError here
    floor_ok = value >= 0 if allow_zero else value > 0
    if not math.isfinite(value) or not floor_ok:
        bound = ">= 0" if allow_zero else "> 0"
        raise ValueError(f"{key} must be a finite number {bound}; got {raw!r}")
    return value


def lifecycle_config_from_env(env: Mapping[str, str]) -> LifecycleConfig | None:
    """Parse the lifecycle env vars. Returns ``None`` when ``MUSIC3_LIFECYCLE_ENABLED`` is not truthy. Pure.

    Defaults are seeded from S1's measurements (docs/session_1_measurements.md §4-5): cold start 87 s ->
    readiness timeout 180 s (margin for one-time JIT / CUDA-graph capture variance); idle 600 s; poll 3 s.
    Non-finite or non-positive readiness/poll durations, and negative/non-finite idle intervals, are
    rejected here (CR-2) so they cannot remove the bound or spin the poller at runtime.
    """
    if not _truthy(env.get("MUSIC3_LIFECYCLE_ENABLED")):
        return None
    return LifecycleConfig(
        socket=env.get("MUSIC3_DOCKER_SOCKET", DEFAULT_SOCKET),
        readiness_timeout_seconds=_duration(env, "MUSIC3_READINESS_TIMEOUT_SECONDS", "180", allow_zero=False),
        idle_timeout_seconds=_duration(env, "MUSIC3_IDLE_TIMEOUT_SECONDS", "600", allow_zero=True),
        poll_seconds=_duration(env, "MUSIC3_READINESS_POLL_SECONDS", "3", allow_zero=False),
    )


def controller_from_env(env: Mapping[str, str]) -> tuple[DockerController, LifecycleConfig] | None:
    """Build a real controller + its config from an env mapping, or ``None`` when lifecycle is disabled.

    Pure over ``env`` (the one ``os.environ`` read happens at the call site in ``readiness.py``).
    """
    config = lifecycle_config_from_env(env)
    if config is None:
        return None
    return DockerController(socket=config.socket), config
