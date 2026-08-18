"""Atomic, exclusive write protocol (spec: artifact-write.md; INV-9 storage side).

Exactly one audio + one sidecar per generation, written audio-first so a crash never leaves a sidecar
claiming success. An exclusive directory makes a job-id collision a hard error, never an overwrite.
"""

import io
import os
import wave

import pytest

from jobs.artifacts import write_artifact
from jobs.store import JobRecord, JobStatus, Submission

_AUDIO = "audio.wav"
_SIDECAR = "sidecar.json"
# Opaque engine-body provenance the runner passes in (CR-1); recorded verbatim, not interpreted here.
_T = {"model": "m", "input": "hi", "instructions": "c", "response_format": "wav", "seed": 0,
      "max_new_tokens": 10, "stream": False}


def _wav(*, rate: int = 44100, channels: int = 2, frames: int = 100) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * channels * frames)
    return buf.getvalue()


def _record(job_id: str = "job0001", audio: bytes | None = None) -> JobRecord:
    return JobRecord(
        id=job_id,
        submission=Submission(input="hi", instructions="c", seed=0, max_new_tokens=10),
        status=JobStatus.succeeded,
        submitted_at="2026-08-15T00:00:00+00:00",
        started_at="2026-08-15T00:00:01+00:00",
        ended_at="2026-08-15T00:00:02+00:00",
        engine="sglang-omni",
        model="MiniMaxAI/MiniMax-Music3",
        audio=_wav() if audio is None else audio,
        content_type="audio/wav",
        generation_seconds=1.0,
    )


def test_completed_generation_yields_exactly_one_pair(tmp_path) -> None:
    root = str(tmp_path)
    job_dir = write_artifact(_record(), root, translated=_T)
    entries = sorted(os.listdir(job_dir))
    assert entries == [_AUDIO, _SIDECAR]          # exactly two, no *.tmp remnants


def test_job_id_collision_is_a_hard_error(tmp_path) -> None:
    root = str(tmp_path)
    write_artifact(_record(audio=_wav(frames=10)), root, translated=_T)
    original = (tmp_path / "job0001" / _AUDIO).read_bytes()
    with pytest.raises(FileExistsError):
        write_artifact(_record(audio=_wav(frames=999)), root, translated=_T)   # same id, different audio
    assert (tmp_path / "job0001" / _AUDIO).read_bytes() == original   # first artifact untouched


def test_incomplete_directory_is_not_complete(tmp_path) -> None:
    root = str(tmp_path)
    job_dir = write_artifact(_record(), root, translated=_T)
    os.remove(os.path.join(job_dir, _SIDECAR))    # simulate a crash between the two renames
    assert not os.path.exists(os.path.join(job_dir, _SIDECAR))   # incomplete: sidecar gone
    assert os.path.exists(os.path.join(job_dir, _AUDIO))


def test_crash_before_audio_rename_leaves_only_tmp_and_no_false_success(tmp_path, monkeypatch) -> None:
    # Make the FIRST os.replace (the audio rename) fail, as a mid-write crash would.
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated crash before audio rename")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    root = str(tmp_path)
    with pytest.raises(OSError, match="simulated crash"):
        write_artifact(_record(), root, translated=_T)
    job_dir = tmp_path / "job0001"
    assert not (job_dir / _AUDIO).exists()        # no servable audio
    assert not (job_dir / _SIDECAR).exists()       # NO sidecar claiming success (adversarial 8)


def test_non_wav_bytes_raise_before_creating_directory(tmp_path) -> None:
    root = str(tmp_path)
    with pytest.raises(wave.Error):   # measure_wav rejects a non-WAV body before the dir is created
        write_artifact(_record(audio=b"not a wav"), root, translated=_T)
    assert not (tmp_path / "job0001").exists()     # nothing partial left behind


def test_missing_audio_raises(tmp_path) -> None:
    with pytest.raises(ValueError, match="no audio"):
        write_artifact(_record(audio=b""), str(tmp_path), translated=_T)
