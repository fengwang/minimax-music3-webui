"""Ingress-level INV-5 tests (codex TS-1, adversarial case 1).

A container name placed in a request field or header must have nowhere to arrive. This proves it AT THE
HTTP EDGE (the earlier controller tests only introspected the controller): the native ``/jobs`` route
refuses an unknown field (422); the cloud translate layer discards unknown fields so none is ever recorded
or forwarded; and in every case the controller stays parameterless, so no request value can select a
container before a socket call.

Deliberate scope note (pushback on part of TS-1): the cloud route DISCARDS an unknown field rather than
hard-refusing it, because S4 deliberately ignores unknown cloud fields for forward-compat (a real cloud
client must not be hard-rejected for sending a field the local engine does not model). INV-5 holds either
way — the discarded value cannot reach the parameterless controller — so refusal is enforced where the
schema is closed (``/jobs``) and inertness is proven where it is open (the cloud edge).
"""

from fastapi.testclient import TestClient

from app.main import config_from_env, create_app
from compat.minimax import translate
from fakes import FakeEngineClient, FakeReadiness

_VALID_CLOUD = {"model": "music-3.0", "prompt": "a happy tune", "lyrics": "[Verse]\nla la"}
_VALID_NATIVE = {"input": "[Verse]\nla la", "instructions": "a happy tune", "max_new_tokens": 250}
_NAME = "minimax-music3-sglang"


def _client() -> TestClient:
    app = create_app(config=config_from_env({}), engine=FakeEngineClient(), readiness=FakeReadiness())
    return TestClient(app)


def test_cloud_translate_discards_a_container_name_field() -> None:
    local = translate({**_VALID_CLOUD, "container_name": _NAME})
    assert "container_name" not in local.cloud  # dropped, never recorded in the sidecar
    assert not hasattr(local, "container_name")  # the translated request carries no container selector
    # The controller (status/start/stop) takes no name parameter, so even the discarded value has no path
    # to a socket call — proven structurally in test_lifecycle_controller.py.


def test_native_jobs_refuses_a_container_name_field() -> None:
    with _client() as client:
        resp = client.post("/jobs", json={**_VALID_NATIVE, "container_name": _NAME})
    assert resp.status_code == 422  # extra="forbid" refuses the unknown field before any processing


def test_a_container_name_header_has_nowhere_to_arrive() -> None:
    with _client() as client:
        resp = client.post(
            "/jobs",
            json=_VALID_NATIVE,
            headers={"X-Container-Name": _NAME, "container-name": _NAME},
        )
    assert resp.status_code == 202  # the header is ignored (no code reads it) — the request proceeds normally


def test_cloud_container_name_field_does_not_leak_into_the_sidecar() -> None:
    local = translate({**_VALID_CLOUD, "container_name": _NAME})
    assert set(local.cloud).issubset({"model", "prompt", "lyrics"})  # only recognised cloud fields recorded
