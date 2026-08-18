"""ui-suite fixtures + the collection guard that keeps the default gate Playwright-free.

The HIGHEST-VALUE behaviour lives at import time: when Playwright is not importable, the ui test modules
are NOT collected, so `uv run pytest -m "not gpu"` — which SELECTS ui tests, since they are not marked
gpu — cannot raise a collection error and stays exactly as cheap as before (C2-1). Not-collecting is
stronger than importorskip: nothing under tests/ui/ is imported at all when the browser package is absent.
The inherited gate command therefore stays literally `-m "not gpu"`; it is never redefined to
`-m "not gpu and not ui"`.

The root tests/conftest.py is left byte-unchanged: the `ui` marker is registered in pyproject.toml, and
collection + fixtures live here. Every test collected under tests/ui/ is auto-marked `ui`, so a later
session cannot add a UI test the `-m ui` selection misses. Fixtures import Playwright lazily so this file
imports cleanly even when the browser package is absent.
"""
from __future__ import annotations

import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest

_UI_DIR = Path(__file__).parent

# --- collection guard: never import a ui test module when Playwright is absent ---
if importlib.util.find_spec("playwright") is None:
    collect_ignore_glob = ["test_*.py"]


def pytest_collection_modifyitems(config, items):
    """Auto-mark every test under tests/ui/ with `ui` (registered in pyproject.toml)."""
    ui_marker = pytest.mark.ui
    for item in items:
        try:
            item.path.relative_to(_UI_DIR)
        except ValueError:
            continue
        item.add_marker(ui_marker)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="session")
def stub_server():
    """A real HTTP server (uvicorn in a daemon thread) wrapping the Strategy-B stub app, so the browser
    loads a real origin whose static serving and route precedence match production. Session-scoped: the
    ~0.5 s startup is paid once; per-test isolation comes from the function-scoped `stub` reset."""
    import uvicorn
    from stub_backend import create_stub_app

    app = create_stub_app()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)  # bounded poll on uvicorn's own ready flag, not a fixed settle sleep
    if not server.started:
        raise RuntimeError("stub server did not start within 15 s")
    try:
        yield app, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


@pytest.fixture(scope="session")
def _browser():
    """One headless Chromium for the session. Launched with the default sandbox; --no-sandbox is added
    only if an unsandboxed root/container launch requires it (recorded in the handoff)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def stub(stub_server):
    """Reset the stub backend state before each test so cases cannot leak into one another."""
    from stub_backend import StubState

    app, _base_url = stub_server
    app.state.stub = StubState()
    return app.state.stub


@pytest.fixture
def base_url(stub_server):
    """The origin the browser navigates to (http://127.0.0.1:<ephemeral>)."""
    return stub_server[1]


@pytest.fixture
def page(_browser, stub):
    """A fresh page + context per test. `stub` (function-scoped) resets backend state first; tests set any
    per-case stub state and then call page.goto(base_url)."""
    context = _browser.new_context()
    pg = context.new_page()
    try:
        yield pg
    finally:
        context.close()
