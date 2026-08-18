"""Serving + listing routes (spec: artifact-serving.md; INV-8 serving side).

The router is exercised against a minimal app whose ``state.config.artifacts_dir`` points at a tmp root,
so these tests do not depend on the create_app wiring (task 5). Every path the router touches goes through
the resolution choke point in ``jobs.artifacts``.
"""

import io
import json
import os
import wave
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routes.artifacts import router
from jobs.artifacts import JobIdError, open_member, write_artifact
from jobs.store import JobRecord, JobStatus, Submission

_T = {"model": "m", "response_format": "wav", "stream": False}


def _wav(frames: int = 100) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 2 * frames)
    return buf.getvalue()


def _record(job_id: str, frames: int = 100) -> JobRecord:
    return JobRecord(
        id=job_id,
        submission=Submission(input="hi", instructions="c", seed=0, max_new_tokens=10),
        status=JobStatus.succeeded,
        submitted_at="2026-08-15T00:00:00+00:00",
        started_at="2026-08-15T00:00:01+00:00",
        ended_at="2026-08-15T00:00:02+00:00",
        engine="sglang-omni",
        model="MiniMaxAI/MiniMax-Music3",
        audio=_wav(frames),
        content_type="audio/wav",
        generation_seconds=1.0,
    )


def _client(root) -> AsyncClient:
    app = FastAPI()
    app.include_router(router)
    app.state.config = SimpleNamespace(artifacts_dir=root)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_open_member_refuses_traversal_id_before_touching_disk(tmp_path) -> None:
    with pytest.raises(JobIdError):
        open_member(str(tmp_path), "..", "audio.wav")


async def test_serves_a_completed_artifact(tmp_path) -> None:
    root = str(tmp_path)
    audio = _wav(200)
    write_artifact(_record("jobAAAA1", frames=200), root, translated=_T)
    async with _client(root) as c:
        r = await c.get("/artifacts/jobAAAA1/audio.wav")
        assert r.status_code == 200
        assert r.content == audio
        assert r.headers["content-type"].startswith("audio/")
        s = await c.get("/artifacts/jobAAAA1/sidecar.json")
        assert s.status_code == 200
        assert json.loads(s.content)["job_id"] == "jobAAAA1"


async def test_incomplete_directory_is_not_served(tmp_path) -> None:
    root = str(tmp_path)
    job_dir = write_artifact(_record("jobINCMP"), root, translated=_T)
    os.remove(os.path.join(job_dir, "sidecar.json"))   # crash between the two renames
    async with _client(root) as c:
        assert (await c.get("/artifacts/jobINCMP/audio.wav")).status_code == 404


async def test_bad_name_and_bad_id_and_missing_return_404(tmp_path) -> None:
    root = str(tmp_path)
    write_artifact(_record("jobREAL1"), root, translated=_T)
    async with _client(root) as c:
        assert (await c.get("/artifacts/jobREAL1/secret.txt")).status_code == 404   # name allow-list
        assert (await c.get("/artifacts/bad.id/audio.wav")).status_code == 404       # invalid id (dot)
        assert (await c.get("/artifacts/nonexistent/audio.wav")).status_code == 404  # unknown id


async def test_listing_is_newest_first_and_complete_only(tmp_path) -> None:
    root = str(tmp_path)
    for i, jid in enumerate(["jobOLD01", "jobMID01", "jobNEW01"]):
        d = write_artifact(_record(jid), root, translated=_T)
        os.utime(d, (1000 + i * 10, 1000 + i * 10))     # force distinct mtimes: OLD < MID < NEW
    incomplete = write_artifact(_record("jobHALF1"), root, translated=_T)
    os.remove(os.path.join(incomplete, "audio.wav"))     # now incomplete
    async with _client(root) as c:
        body = (await c.get("/artifacts")).json()
    ids = [e["job_id"] for e in body]
    assert ids == ["jobNEW01", "jobMID01", "jobOLD01"]   # newest first
    assert "jobHALF1" not in ids                          # incomplete omitted
    assert body[0]["audio_url"] == "/artifacts/jobNEW01/audio.wav"
    assert body[0]["output"]["sample_rate"] == 44100      # sidecar summary surfaced


async def test_unconfigured_root_lists_empty_and_404s(tmp_path) -> None:
    async with _client(None) as c:
        assert (await c.get("/artifacts")).json() == []
        assert (await c.get("/artifacts/jobREAL1/audio.wav")).status_code == 404


async def test_symlinked_audio_member_is_refused_and_unlisted(tmp_path) -> None:
    # SEC-1/COR-1: swap audio.wav for a symlink escaping the root (as a TOCTOU attacker would). open_member
    # opens with O_NOFOLLOW, so the swap is refused (404) and the dir is not listed — refused by the
    # resolver, not by file permissions.
    root = str(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("s3cr3t")
    job_dir = write_artifact(_record("jobSYM01"), root, translated=_T)
    audio = os.path.join(job_dir, "audio.wav")
    os.remove(audio)
    os.symlink(secret, audio)
    async with _client(root) as c:
        assert (await c.get("/artifacts/jobSYM01/audio.wav")).status_code == 404   # not served
        assert (await c.get("/artifacts")).json() == []                            # not listed
    assert secret.read_text() == "s3cr3t"   # never streamed/truncated


async def test_symlinked_sidecar_makes_pair_incomplete(tmp_path) -> None:
    # COR-1: a symlinked sidecar must not let the audio be observable without a safe sidecar.
    root = str(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text('{"x": 1}')
    job_dir = write_artifact(_record("jobSYM02"), root, translated=_T)
    sidecar = os.path.join(job_dir, "sidecar.json")
    os.remove(sidecar)
    os.symlink(outside, sidecar)
    async with _client(root) as c:
        assert (await c.get("/artifacts/jobSYM02/audio.wav")).status_code == 404    # pair incomplete
        assert (await c.get("/artifacts/jobSYM02/sidecar.json")).status_code == 404
        assert (await c.get("/artifacts")).json() == []


async def test_symlinked_job_directory_is_refused(tmp_path) -> None:
    # SEC-1: a symlinked job directory planted in the root must be refused by O_NOFOLLOW|O_DIRECTORY.
    root = tmp_path / "root"
    root.mkdir()
    real = write_artifact(_record("jobREAL9"), str(root), translated=_T)
    os.symlink(real, root / "jobLINK9")
    async with _client(str(root)) as c:
        assert (await c.get("/artifacts/jobLINK9/audio.wav")).status_code == 404
        ids = [e["job_id"] for e in (await c.get("/artifacts")).json()]
    assert "jobLINK9" not in ids and "jobREAL9" in ids
