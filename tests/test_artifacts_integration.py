"""Runner -> artifact persistence, end to end (spec: artifact-write.md runner requirement).

Drives the real app (create_app + the single worker) with a local engine double, so a successful native
generation lands exactly one audio+sidecar pair on disk that is then served and listed, and a write
failure fails the job with no sidecar claiming success (adversarial case 8).
"""

import asyncio
import io
import os
import wave

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import config_from_env, create_app
from engines.sglang.client import EngineRequest, EngineResult
from lifecycle.readiness import ProbeOutcome, Readiness

_VALID = {"input": "[Verse]\nhi", "instructions": "warm caption", "seed": 0, "max_new_tokens": 250}


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 2 * 500)
    return buf.getvalue()


class _Engine:
    """A local EngineClient double returning fixed bytes (valid WAV, or deliberately not)."""

    def __init__(self, audio: bytes) -> None:
        self._audio = audio

    async def generate(self, request: EngineRequest) -> EngineResult:
        return EngineResult(
            audio=self._audio, content_type="audio/wav", engine="sglang-omni",
            model=request.model, generation_seconds=0.01,
        )


class _SeqEngine:
    """Returns a different audio body per call (last one repeats), to test worker recovery across jobs."""

    def __init__(self, audios: list[bytes]) -> None:
        self._audios = audios
        self._i = 0

    async def generate(self, request: EngineRequest) -> EngineResult:
        audio = self._audios[min(self._i, len(self._audios) - 1)]
        self._i += 1
        return EngineResult(
            audio=audio, content_type="audio/wav", engine="sglang-omni",
            model=request.model, generation_seconds=0.01,
        )


async def _ready() -> ProbeOutcome:
    return ProbeOutcome.MODELS_AVAILABLE


def _app(tmp_path, engine):
    return create_app(
        config=config_from_env({"MUSIC3_ARTIFACTS_DIR": str(tmp_path)}),
        engine=engine,
        readiness=Readiness(_ready),
    )


async def _drive(client: AsyncClient, job_id: str) -> str:
    for _ in range(400):
        status = (await client.get(f"/jobs/{job_id}")).json()["status"]
        if status in ("succeeded", "failed", "cancelled"):
            return status
        await asyncio.sleep(0.005)
    raise AssertionError("job did not reach a terminal state")


async def test_successful_generation_persists_serves_and_lists_one_pair(tmp_path) -> None:
    app = _app(tmp_path, _Engine(_wav()))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        job = (await client.post("/jobs", json=_VALID)).json()
        assert await _drive(client, job["id"]) == "succeeded"

        job_dir = tmp_path / job["id"]
        assert sorted(os.listdir(job_dir)) == ["audio.wav", "sidecar.json"]   # exactly one pair

        listing = (await client.get("/artifacts")).json()
        assert [entry["job_id"] for entry in listing] == [job["id"]]

        audio = await client.get(f"/artifacts/{job['id']}/audio.wav")
        assert audio.status_code == 200
        assert audio.content == _wav()


async def test_non_wav_result_fails_job_and_writes_no_artifact(tmp_path) -> None:
    app = _app(tmp_path, _Engine(b"not a wav"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        job = (await client.post("/jobs", json=_VALID)).json()
        assert await _drive(client, job["id"]) == "failed"
        record = (await client.get(f"/jobs/{job['id']}")).json()
        assert "artifact write failed" in (record["error"] or "")
        assert not (tmp_path / job["id"]).exists()                 # nothing partial, no false-success
        assert (await client.get("/artifacts")).json() == []


async def test_truncated_wav_fails_job_and_worker_still_drains_next(tmp_path) -> None:
    # F1 regression: a truncated 2xx body makes measure_wav fail. It must fail the JOB (not crash the
    # worker with an uncaught EOFError); the worker must then process the next job to completion.
    truncated = _wav()[:20]
    app = _app(tmp_path, _SeqEngine([truncated, _wav()]))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        first = (await client.post("/jobs", json=_VALID)).json()
        assert await _drive(client, first["id"]) == "failed"        # not stuck running
        assert "artifact write failed" in ((await client.get(f"/jobs/{first['id']}")).json()["error"] or "")

        second = (await client.post("/jobs", json=_VALID)).json()
        assert await _drive(client, second["id"]) == "succeeded"     # worker survived and recovered
        assert (tmp_path / second["id"] / "audio.wav").exists()


async def test_midwrite_oserror_fails_job_with_no_false_success(tmp_path, monkeypatch) -> None:
    # A genuine post-directory write failure (e.g. disk full): the job must fail, never succeed.
    from jobs import artifacts

    def boom(record, root, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(artifacts, "write_artifact", boom)
    app = _app(tmp_path, _Engine(_wav()))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        job = (await client.post("/jobs", json=_VALID)).json()
        assert await _drive(client, job["id"]) == "failed"
        record = (await client.get(f"/jobs/{job['id']}")).json()
        assert "artifact write failed" in (record["error"] or "")
        assert "No space left" in record["error"]


@pytest.mark.parametrize("engine_audio", [_wav()])
async def test_no_artifacts_root_keeps_s2_behaviour(tmp_path, engine_audio) -> None:
    # artifacts_dir unset → the job still succeeds and nothing is written (S2 behaviour preserved).
    app = create_app(config=config_from_env({}), engine=_Engine(engine_audio), readiness=Readiness(_ready))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        job = (await client.post("/jobs", json=_VALID)).json()
        assert await _drive(client, job["id"]) == "succeeded"
        assert (await client.get("/artifacts")).json() == []
