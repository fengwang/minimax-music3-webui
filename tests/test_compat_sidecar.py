"""Sidecar gains the cloud envelope + trace_id (A-S4-01, INV-9 for S4). spec: cloud-compat-envelope.

Asserts the amendment is additive: with a cloud envelope it is recorded under request.cloud; without one
(the native path) the request object is exactly S3's, and trace_id is always present.
"""

from jobs.artifacts import WavFacts, build_sidecar
from jobs.store import JobRecord, JobStatus, Submission

_DEPLOY = {"version": "v1", "image_digest": None, "model_path": None}
_FACTS = WavFacts(sample_rate=32000, channels=2, duration_seconds=10.0, byte_size=640000, content_hash="ab")
_TRANSLATED = {"model": "MiniMaxAI/MiniMax-Music3", "input": "i", "instructions": "c"}
_CLOUD = {
    "model": "music-3.0",
    "prompt": "c",
    "lyrics": "i",
    "output_format": "url",
    "seed": 7,
    "max_new_tokens": 250,
    "audio_setting": {"format": "mp3", "sample_rate": 44100, "bitrate": 256000},
}


def _record(cloud: dict | None) -> JobRecord:
    return JobRecord(
        id="job42",
        submission=Submission(input="i", instructions="c", seed=7, max_new_tokens=250, cloud=cloud),
        status=JobStatus.succeeded,
        submitted_at="2026-01-01T00:00:00+00:00",
        started_at="2026-01-01T00:00:01+00:00",
        ended_at="2026-01-01T00:00:05+00:00",
        engine="sglang-omni",
        model="MiniMaxAI/MiniMax-Music3",
        content_type="audio/wav",
        generation_seconds=0.01,
    )


def test_sidecar_records_cloud_envelope_and_trace_id() -> None:
    sc = build_sidecar(_record(_CLOUD), _DEPLOY, _FACTS, _TRANSLATED)
    assert sc["trace_id"] == "job42"  # stable id shared with the cloud response (§5)
    assert sc["request"]["cloud"] == _CLOUD  # the received envelope, recorded verbatim (INV-9)
    # native/translated provenance still present alongside the cloud envelope
    assert sc["request"]["seed"] == 7
    assert sc["request"]["translated"] == _TRANSLATED


def test_native_sidecar_is_s3_shape_plus_trace_id() -> None:
    sc = build_sidecar(_record(None), _DEPLOY, _FACTS, _TRANSLATED)
    assert sc["trace_id"] == "job42"  # trace_id is always present (additive)
    assert "cloud" not in sc["request"]  # native path: no cloud key -> identical to S3's request object
