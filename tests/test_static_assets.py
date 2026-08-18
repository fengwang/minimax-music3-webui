"""Static-asset serving + the StaticFiles mount must not shadow the API (spec: static-ui-shell.md).

CPU-only. Fakes are injected so no engine or GPU is touched; the lifespan (worker) is deliberately not
run — these tests exercise serving and route precedence, not job execution.
"""

from httpx import ASGITransport, AsyncClient

from app.main import config_from_env, create_app
from fakes import FakeEngineClient
from lifecycle.readiness import ProbeOutcome, Readiness


async def _ready_probe() -> ProbeOutcome:
    return ProbeOutcome.MODELS_AVAILABLE


def _app() -> object:
    return create_app(
        config=config_from_env({}),
        engine=FakeEngineClient(default="succeed"),
        readiness=Readiness(_ready_probe),
    )


async def test_root_serves_index_html() -> None:
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "MiniMax-Music3" in r.text  # attribution present in the served body (INV-11)


async def test_style_and_app_js_served() -> None:
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        css = await client.get("/style.css")
        js = await client.get("/app.js")
    assert css.status_code == 200 and "css" in css.headers["content-type"]
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]


async def test_mount_does_not_shadow_api() -> None:
    # The catch-all static mount is registered last, so the S2/S3/S4/S5 routes and /openapi.json win.
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        health = await client.get("/health")
        assert health.status_code == 200 and "engine" in health.json()
        submit = await client.post(
            "/jobs", json={"input": "x", "instructions": "y", "max_new_tokens": 250}
        )
        assert submit.status_code == 202
        assert (await client.get("/artifacts")).status_code == 200
        # blocking cloud route unchanged: an empty body is a 422 validation error, never a static 404
        assert (await client.post("/v1/music_generation", json={})).status_code != 404
        assert (await client.get("/openapi.json")).status_code == 200
