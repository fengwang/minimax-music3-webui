"""GPU smoke (EV-G seed): one real 250-frame job end-to-end against the live engine.

Deselected by default (``pytest -m "not gpu"``); runs only on the RTX 5090 host with a reachable engine:

    MUSIC3_ENGINE_BASE_URL=http://127.0.0.1:8000 uv run pytest -m gpu

Its presence also proves the ``gpu`` marker is deselected on the CPU host loop (spec: python-tooling.md).
Seeds S7's EV-G execution; wired against the real ``SglangClient`` via the app's default composition.
"""

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import config_from_env, create_app

pytestmark = pytest.mark.gpu


async def test_real_engine_250_frame_job_succeeds() -> None:
    app = create_app(config=config_from_env(os.environ))
    payload = {
        "input": "[Verse]\nhello world tonight",
        "instructions": "a warm acoustic ballad, female vocal, gentle guitar",
        "seed": 0,
        "max_new_tokens": 250,
    }
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=1300.0) as client,
    ):
        job = (await client.post("/jobs", json=payload)).json()
        status = None
        for _ in range(2600):  # up to ~1300 s at a 0.5 s poll interval (D9/E-16 read-timeout floor)
            status = (await client.get(f"/jobs/{job['id']}")).json()["status"]
            if status in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.5)
    assert status == "succeeded"
