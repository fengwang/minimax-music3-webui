"""POST /v1/music_generation + GET .../result/{trace_id} HTTP behaviour (specs: all three).

CPU-only. Success paths need a valid WAV from the engine, so a local WAV-returning fake is defined here
(tests/fakes.py is outside this session's blast radius and its fake returns non-WAV bytes). Refusal and
local-failure paths reuse the shared fakes. Transcode runs real ffmpeg; skipped only if it is absent.
"""

import asyncio
import io
import shutil
import struct
import wave
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routes.music import MAX_CONCURRENT_DELIVERIES, MAX_REQUEST_BODY_BYTES
from app.routes.music import router as music_router
from engines.sglang.client import EngineError, EngineResult
from fakes import FakeReadiness
from jobs.runner import JobRunner
from jobs.store import JobStatus, JobStore, Submission

_MIN = {"model": "music-3.0", "prompt": "warm caption", "lyrics": "[Verse]\nhi"}
_needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe absent"
)


def _wav_bytes(seconds: float = 0.1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(32000)
        w.writeframes(b"".join(struct.pack("<h", (i % 200) - 100) * 2 for i in range(int(seconds * 32000))))
    return buf.getvalue()


class WavEngine:
    """A valid-WAV-returning engine double. Tracks concurrency so INV-1 is provable."""

    def __init__(self, *, delay: float = 0.0, seconds: float = 0.1) -> None:
        self._audio = _wav_bytes(seconds)
        self._delay = delay
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def generate(self, request) -> EngineResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            else:
                await asyncio.sleep(0)
            return EngineResult(
                audio=self._audio, content_type="audio/wav", engine="fake",
                model=request.model, generation_seconds=0.01,
            )
        finally:
            self.active -= 1


class RaisingEngine:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request) -> EngineResult:
        self.calls += 1
        raise EngineError("boom")


def _make_app(artifacts_dir, engine) -> tuple[FastAPI, JobStore, JobRunner]:
    root = str(artifacts_dir) if artifacts_dir is not None else None  # None => persistence off
    app = FastAPI()
    store = JobStore()
    readiness = FakeReadiness()
    runner = JobRunner(
        store, engine, model="MiniMaxAI/MiniMax-Music3",
        clock=lambda: "2026-01-01T00:00:00+00:00", readiness=readiness, artifacts_root=root,
    )
    app.state.store = store
    app.state.runner = runner
    app.state.readiness = readiness
    app.state.config = SimpleNamespace(artifacts_dir=root)
    app.state.delivery_slots = asyncio.Semaphore(MAX_CONCURRENT_DELIVERIES)  # matches create_app (TP-1)
    app.include_router(music_router)
    return app, store, runner


async def _wait_succeeded(store: JobStore, job_id: str, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if store.get(job_id).status is JobStatus.succeeded:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} never succeeded; status={store.get(job_id).status}")


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({**_MIN, "stream": True}, "stream"),
        ({**_MIN, "lyrics_optimizer": True}, "lyrics_optimizer"),
        ({**_MIN, "is_instrumental": True}, "is_instrumental"),
        ({**_MIN, "cover_feature_id": "x"}, "cover_feature_id"),
        ({**_MIN, "audio_url": "http://x/a.wav"}, "audio_url"),
        ({**_MIN, "audio_base64": "AAAA"}, "audio_base64"),
        ({**_MIN, "model": "music-2.6"}, "model"),
        ({**_MIN, "model": "music-cover"}, "model"),
        ({**_MIN, "audio_setting": {"format": "pcm", "bitrate": 128000}}, "bitrate"),
        ({**_MIN, "audio_setting": {"format": "wav", "bitrate": 128000}}, "bitrate"),
        ({**_MIN, "max_new_tokens": 9001}, "max_new_tokens"),
        ({**_MIN, "prompt": "x" * 2001}, "prompt"),
        ({**_MIN, "lyrics": ""}, "lyrics"),
    ],
)
async def test_refusals_are_http_400_code_2013_naming_field(tmp_path, body, field) -> None:
    engine = WavEngine()
    app, _store, _runner = _make_app(tmp_path, engine)  # no worker: refused before the queue
    async with _client(app) as client:
        r = await client.post("/v1/music_generation", json=body)
    assert r.status_code == 400, f"{body} expected 400"
    envelope = r.json()
    assert envelope["base_resp"]["status_code"] == 2013
    assert field in envelope["base_resp"]["status_msg"]
    assert envelope["data"] is None
    assert engine.calls == 0  # nothing dispatched


async def test_invalid_json_body_is_400_2013(tmp_path) -> None:
    app, _store, _runner = _make_app(tmp_path, WavEngine())
    async with _client(app) as client:
        r = await client.post(
            "/v1/music_generation", content=b"not json", headers={"content-type": "application/json"}
        )
    assert r.status_code == 400 and r.json()["base_resp"]["status_code"] == 2013


async def test_no_refusal_returns_http_200(tmp_path) -> None:
    # sanity: the whole refusal set is non-200 (acceptance criterion "no member returns HTTP 200")
    app, _store, _runner = _make_app(tmp_path, WavEngine())
    async with _client(app) as client:
        for body in ({**_MIN, "stream": True}, {**_MIN, "model": "zzz"}, {**_MIN, "lyrics": ""}):
            assert (await client.post("/v1/music_generation", json=body)).status_code != 200


@_needs_ffmpeg
async def test_success_url_envelope_and_trace_linkage(tmp_path) -> None:
    engine = WavEngine()
    app, store, runner = _make_app(tmp_path, engine)
    runner.start()
    try:
        async with _client(app) as client:
            r = await client.post("/v1/music_generation", json={**_MIN, "output_format": "url"})
            assert r.status_code == 200
            body = r.json()
            assert body["base_resp"] == {"status_code": 0, "status_msg": "success"}
            assert body["analysis_info"] is None
            assert body["data"]["status"] == 2
            trace = body["trace_id"]
            assert body["data"]["audio"].endswith(f"/v1/music_generation/result/{trace}")
            for key in ("music_duration", "music_sample_rate", "music_channel", "bitrate", "music_size"):
                assert key in body["extra_info"]
            assert body["extra_info"]["music_sample_rate"] == 32000
            # trace_id links to the artifact sidecar (INV-9)
            assert store.get(trace).status is JobStatus.succeeded
    finally:
        await runner.stop()


@_needs_ffmpeg
async def test_hex_and_url_are_byte_identical(tmp_path) -> None:
    engine = WavEngine()
    app, _store, runner = _make_app(tmp_path, engine)
    runner.start()
    try:
        async with _client(app) as client:
            r_url = await client.post("/v1/music_generation", json={**_MIN, "output_format": "url"})
            url = r_url.json()["data"]["audio"]
            url_bytes = (await client.get(url)).content
            r_hex = await client.post("/v1/music_generation", json={**_MIN, "output_format": "hex"})
            audio_hex = r_hex.json()["data"]["audio"]
        assert bytes.fromhex(audio_hex) == url_bytes  # one deterministic transcode -> identical bytes
    finally:
        await runner.stop()


@_needs_ffmpeg
async def test_extra_info_measured_matches_delivered_file(tmp_path) -> None:
    engine = WavEngine()
    app, _store, runner = _make_app(tmp_path, engine)
    runner.start()
    try:
        async with _client(app) as client:
            r = await client.post(
                "/v1/music_generation",
                json={**_MIN, "output_format": "url", "audio_setting": {"format": "mp3", "bitrate": 128000}},
            )
            body = r.json()
            delivered = (await client.get(body["data"]["audio"])).content
        info = body["extra_info"]
        assert info["music_size"] == len(delivered)  # measured from the delivered file, not assumed
        assert info["music_channel"] == 2
        assert info["music_sample_rate"] == 32000  # mp3 with no sample_rate -> native default
        assert info["music_duration"] > 0
        assert info["bitrate"] > 0
    finally:
        await runner.stop()


async def test_engine_failure_is_5xx_code_5000(tmp_path) -> None:
    engine = RaisingEngine()
    app, _store, runner = _make_app(tmp_path, engine)
    runner.start()
    try:
        async with _client(app) as client:
            r = await client.post("/v1/music_generation", json=_MIN)
        assert r.status_code >= 500
        body = r.json()
        assert body["base_resp"]["status_code"] == 5000  # reserved local code, never 2013
        assert body["trace_id"]  # a job existed, so the failure is traceable
    finally:
        await runner.stop()


@_needs_ffmpeg
async def test_two_overlapping_submits_serialise_inv1(tmp_path) -> None:
    engine = WavEngine(delay=0.05)
    app, _store, runner = _make_app(tmp_path, engine)
    runner.start()
    try:
        async with _client(app) as client:
            r1, r2 = await asyncio.gather(
                client.post("/v1/music_generation", json={**_MIN, "output_format": "url"}),
                client.post("/v1/music_generation", json={**_MIN, "output_format": "url"}),
            )
        assert r1.status_code == 200 and r2.status_code == 200
        assert engine.calls == 2
        assert engine.max_active == 1  # concurrency exactly one (INV-1)
    finally:
        await runner.stop()


async def test_result_route_404_for_crafted_or_missing_id(tmp_path) -> None:
    app, _store, _runner = _make_app(tmp_path, WavEngine())
    async with _client(app) as client:
        assert (await client.get("/v1/music_generation/result/..%2f..%2fetc")).status_code == 404
        assert (await client.get("/v1/music_generation/result/deadbeef")).status_code == 404


@_needs_ffmpeg
async def test_result_route_404_for_native_artifact(tmp_path) -> None:
    # A native (cloud=None) generation persists audio.wav + a sidecar WITHOUT request.cloud; the cloud
    # delivery route must refuse to serve it (guards the S3/S4 storage boundary).
    engine = WavEngine()
    app, store, runner = _make_app(tmp_path, engine)
    runner.start()
    try:
        record = await runner.submit(Submission(input="i", instructions="c", seed=0, max_new_tokens=250))
        await _wait_succeeded(store, record.id)
        async with _client(app) as client:
            r = await client.get(f"/v1/music_generation/result/{record.id}")
        assert r.status_code == 404
    finally:
        await runner.stop()


@_needs_ffmpeg
async def test_result_route_transcode_failure_is_5xx_5000(tmp_path) -> None:
    # If the stored WAV is corrupted between the POST and a later url GET, the on-demand transcode fails;
    # that is a local failure and must surface as the reserved 5000 envelope, not a bare 500 (adversarial).
    engine = WavEngine()
    app, _store, runner = _make_app(tmp_path, engine)
    runner.start()
    try:
        async with _client(app) as client:
            trace = (await client.post("/v1/music_generation", json={**_MIN, "output_format": "url"})).json()[
                "trace_id"
            ]
            (tmp_path / trace / "audio.wav").write_bytes(b"no longer a wav")  # corrupt the source
            got = await client.get(f"/v1/music_generation/result/{trace}")
        assert got.status_code >= 500
        assert got.json()["base_resp"]["status_code"] == 5000
    finally:
        await runner.stop()


@_needs_ffmpeg
async def test_result_route_corrupt_audio_setting_is_5xx_5000(tmp_path) -> None:
    # A parseable-but-invalid recorded audio_setting (stored-artifact corruption) is a local failure and
    # must use the reserved 5000 envelope, not a bare 500 (CR-4).
    import json as _json

    engine = WavEngine()
    app, _store, runner = _make_app(tmp_path, engine)
    runner.start()
    try:
        async with _client(app) as client:
            trace = (await client.post("/v1/music_generation", json={**_MIN, "output_format": "url"})).json()[
                "trace_id"
            ]
            sc_path = tmp_path / trace / "sidecar.json"
            sc = _json.loads(sc_path.read_text())
            sc["request"]["cloud"]["audio_setting"] = {"format": "flac"}  # unresolvable
            sc_path.write_text(_json.dumps(sc))
            got = await client.get(f"/v1/music_generation/result/{trace}")
        assert got.status_code >= 500
        assert got.json()["base_resp"]["status_code"] == 5000
    finally:
        await runner.stop()


async def test_oversize_request_body_is_refused_2013(tmp_path) -> None:
    # SS-1: a body over the size cap is refused (2013) before JSON parse / dispatch.
    engine = WavEngine()
    app, _store, _runner = _make_app(tmp_path, engine)
    big = b'{"model":"music-3.0","prompt":"' + b"a" * (MAX_REQUEST_BODY_BYTES + 1024) + b'","lyrics":"x"}'
    async with _client(app) as client:
        r = await client.post("/v1/music_generation", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["base_resp"]["status_code"] == 2013
    assert engine.calls == 0  # never dispatched


@_needs_ffmpeg
async def test_concurrent_url_deliveries_all_succeed(tmp_path) -> None:
    # TP-1: several concurrent url GETs (bounded by the delivery semaphore) all deliver identical bytes.
    engine = WavEngine()
    app, _store, runner = _make_app(tmp_path, engine)
    runner.start()
    try:
        async with _client(app) as client:
            trace = (await client.post("/v1/music_generation", json={**_MIN, "output_format": "url"})).json()[
                "trace_id"
            ]
            url = f"/v1/music_generation/result/{trace}"
            results = await asyncio.gather(*[client.get(url) for _ in range(4)])
        assert all(r.status_code == 200 for r in results)
        bodies = {r.content for r in results}
        assert len(bodies) == 1 and next(iter(bodies))  # identical, non-empty bytes each time
    finally:
        await runner.stop()


@_needs_ffmpeg
async def test_hex_body_spans_multiple_chunks(tmp_path) -> None:
    # TP-1: a delivered payload larger than the 64 KiB stream chunk exercises multi-chunk hex assembly.
    engine = WavEngine(seconds=0.6)  # ~76 KiB wav -> ~153 KiB hex -> multiple 64 KiB chunks
    app, _store, runner = _make_app(tmp_path, engine)
    runner.start()
    try:
        async with _client(app) as client:
            r_url = await client.post("/v1/music_generation", json={**_MIN, "output_format": "url"})
            url_bytes = (await client.get(r_url.json()["data"]["audio"])).content
            r_hex = await client.post("/v1/music_generation", json={**_MIN, "output_format": "hex"})
        assert len(url_bytes) > 65536  # crosses the 64 KiB chunk boundary
        assert bytes.fromhex(r_hex.json()["data"]["audio"]) == url_bytes
    finally:
        await runner.stop()


@_needs_ffmpeg
@pytest.mark.parametrize(
    "audio_setting", [{"format": "mp3", "bitrate": 128000}, {"format": "pcm", "sample_rate": 24000}]
)
async def test_hex_url_parity_for_mp3_and_pcm(tmp_path, audio_setting) -> None:
    # TP-1: hex and url deliver byte-identical audio for the non-default formats too (deterministic transcode).
    engine = WavEngine()
    app, _store, runner = _make_app(tmp_path, engine)
    runner.start()
    try:
        async with _client(app) as client:
            r_url = await client.post(
                "/v1/music_generation", json={**_MIN, "output_format": "url", "audio_setting": audio_setting}
            )
            url_bytes = (await client.get(r_url.json()["data"]["audio"])).content
            r_hex = await client.post(
                "/v1/music_generation", json={**_MIN, "output_format": "hex", "audio_setting": audio_setting}
            )
        assert bytes.fromhex(r_hex.json()["data"]["audio"]) == url_bytes
    finally:
        await runner.stop()


async def test_post_artifacts_not_configured_is_5xx_5000(tmp_path) -> None:
    # Persistence off: the job succeeds but there is no artifact to deliver -> reserved local code, not 2013.
    engine = WavEngine()
    app, _store, runner = _make_app(None, engine)
    runner.start()
    try:
        async with _client(app) as client:
            r = await client.post("/v1/music_generation", json=_MIN)
        assert r.status_code >= 500
        assert r.json()["base_resp"]["status_code"] == 5000
    finally:
        await runner.stop()
