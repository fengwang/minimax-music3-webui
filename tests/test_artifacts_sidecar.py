"""Sidecar schema + measured output facts (spec: sidecar-schema.md; INV-9).

Output facts are MEASURED from the written WAV, never copied from the model card (E-17). Deployment facts
(engine version, image digest, model path) are env-sourced with an honest non-empty version default.
"""

import io
import wave
from dataclasses import replace

import pytest

from jobs.artifacts import build_sidecar, deployment_facts_from_env, measure_wav
from jobs.store import JobRecord, JobStatus, Submission


def _make_wav(*, rate: int, channels: int, frames: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * channels * frames)
    return buf.getvalue()


_BASE = JobRecord(
    id="abc123",
    submission=Submission(input="[Verse]\nhi", instructions="warm caption", seed=0, max_new_tokens=250),
    status=JobStatus.succeeded,
    submitted_at="2026-08-15T00:00:00+00:00",
    started_at="2026-08-15T00:00:02+00:00",
    ended_at="2026-08-15T00:00:05+00:00",
    engine="sglang-omni",
    model="MiniMaxAI/MiniMax-Music3",
    audio=None,
    content_type="audio/wav",
    generation_seconds=3.0,
)


def _record(**over) -> JobRecord:
    return replace(_BASE, **over)


def test_measure_wav_reports_measured_facts() -> None:
    audio = _make_wav(rate=44100, channels=2, frames=44100)  # exactly 1.0 s
    facts = measure_wav(audio)
    assert facts.sample_rate == 44100
    assert facts.channels == 2
    assert abs(facts.duration_seconds - 1.0) < 1e-9
    assert facts.byte_size == len(audio)
    import hashlib

    assert facts.content_hash == hashlib.sha256(audio).hexdigest()


def test_measure_wav_explicit_bad_bytes_raise_wave_error() -> None:
    valid = _make_wav(rate=44100, channels=2, frames=44100)
    for bad in (b"RIFF", valid[:20], valid[:30], b"not a wav", b""):
        with pytest.raises(wave.Error):
            measure_wav(bad)


def test_measure_wav_never_leaks_a_non_wave_error_on_corruption() -> None:
    # F1 / adversarial verifier: the stdlib parser raises EOFError, struct.error, or a bare RuntimeError
    # (chunk-size overflow) on different malformations — none caught by the single-worker runner. This is
    # the property that keeps the worker alive: measure_wav either returns facts or raises wave.Error, and
    # NEVER any other exception type. Corrupt every header byte and many truncation lengths to exercise it.
    valid = _make_wav(rate=44100, channels=2, frames=64)
    variants = [valid[:n] for n in range(len(valid))]                         # every truncation length
    variants += [valid[:i] + bytes([valid[i] ^ 0xFF]) + valid[i + 1:]         # single-byte flip per header byte
                 for i in range(min(48, len(valid)))]
    # An explicit chunk-size overflow (the RuntimeError trigger): set the data-chunk size to 0xFFFFFFFF.
    idx = valid.index(b"data") + 4
    variants.append(valid[:idx] + b"\xff\xff\xff\xff" + valid[idx + 4:])
    for variant in variants:
        try:
            measure_wav(variant)          # may legitimately succeed for a still-parseable corruption
        except wave.Error:
            pass                           # the only permitted failure type
        # any other exception propagates out of the loop and fails the test (would wedge the worker)


def test_deployment_defaults_and_overrides() -> None:
    d = deployment_facts_from_env({})
    assert d["version"] == "unpinned-dev"        # honest non-empty default (INV-9 non-empty)
    assert d["image_digest"] is None
    assert d["model_path"] is None
    d2 = deployment_facts_from_env(
        {"MUSIC3_ENGINE_VERSION": "sha256:abc", "MUSIC3_IMAGE_DIGEST": "img@sha256:def",
         "MUSIC3_MODEL_PATH": "/models/MiniMax-Music3"}
    )
    assert d2 == {"version": "sha256:abc", "image_digest": "img@sha256:def",
                  "model_path": "/models/MiniMax-Music3"}


_TRANSLATED = {"model": "MiniMaxAI/MiniMax-Music3", "input": "[Verse]\nhi", "instructions": "warm caption",
               "response_format": "wav", "seed": 0, "max_new_tokens": 250, "stream": False}


def test_build_sidecar_has_every_inv9_field_typed() -> None:
    audio = _make_wav(rate=44100, channels=2, frames=22050)  # 0.5 s
    sidecar = build_sidecar(_record(), deployment_facts_from_env({}), measure_wav(audio), _TRANSLATED)

    assert sidecar["engine"]["name"] == "sglang-omni" and sidecar["engine"]["name"]
    assert sidecar["engine"]["version"] == "unpinned-dev" and sidecar["engine"]["version"]
    assert sidecar["model"]["id"] == "MiniMaxAI/MiniMax-Music3"
    req = sidecar["request"]
    assert req["input"] == "[Verse]\nhi"
    assert req["instructions"] == "warm caption"
    assert isinstance(req["seed"], int) and req["seed"] == 0
    assert req["max_new_tokens"] == 250
    # translated engine body is recorded verbatim as passed by the runner (CR-1: plain data in, not
    # reconstructed from engine internals inside the storage module)
    assert req["translated"] == _TRANSLATED
    assert req["translated"]["response_format"] == "wav"
    assert req["translated"]["stream"] is False
    t = sidecar["timings"]
    assert t["submitted_at"] and t["started_at"] and t["ended_at"]
    assert t["generation_seconds"] == 3.0
    assert abs(t["queue_seconds"] - 2.0) < 1e-6      # started - submitted
    assert abs(t["total_seconds"] - 5.0) < 1e-6      # ended - submitted
    out = sidecar["output"]
    assert out["sample_rate"] == 44100
    assert out["channels"] == 2
    assert abs(out["duration_seconds"] - 0.5) < 1e-9
    assert out["byte_size"] == len(audio)
    assert len(out["content_hash"]) == 64


def test_prompt_with_separator_and_nul_lives_only_in_sidecar() -> None:
    audio = _make_wav(rate=32000, channels=1, frames=100)
    nasty = "verse/one\x00two"
    rec = _record(submission=Submission(input=nasty, instructions="c", seed=7, max_new_tokens=10))
    sidecar = build_sidecar(rec, deployment_facts_from_env({}), measure_wav(audio), {"seed": 7})
    assert sidecar["request"]["input"] == nasty     # preserved verbatim, JSON-only
    assert sidecar["request"]["seed"] == 7
