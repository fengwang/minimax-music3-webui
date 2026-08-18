"""ffmpeg transcode + measure Actions (spec: ffmpeg-transcode.md).

Runs a real ffmpeg/ffprobe over a tiny synthetic WAV. Skipped only if the tools are truly absent; the
deploy host and app image both ship them (E-22). Not gpu-marked — transcode is CPU-only.
"""

import os
import shutil
import struct
import wave
from pathlib import Path

import pytest

from transcode.ffmpeg import (
    NATIVE_CHANNELS,
    NATIVE_SAMPLE_RATE,
    TranscodeError,
    measure,
    resolve_transcode,
    transcode,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _write_wav(path: str, seconds: float = 0.1) -> None:
    nframes = int(seconds * NATIVE_SAMPLE_RATE)
    with wave.open(path, "wb") as w:
        w.setnchannels(NATIVE_CHANNELS)
        w.setsampwidth(2)
        w.setframerate(NATIVE_SAMPLE_RATE)
        # a small deterministic ramp so lossy encoding has real signal to work on
        sample = [struct.pack("<h", (i % 1000) - 500) for i in range(nframes)]
        w.writeframes(b"".join(s * NATIVE_CHANNELS for s in sample))


def _fd(path: str) -> int:
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)


def test_transcode_wav_measures_native(tmp_path) -> None:
    src = str(tmp_path / "audio.wav")
    _write_wav(src)
    out = str(tmp_path / "out.wav")
    fd = _fd(src)
    try:
        plan = resolve_transcode({"format": "wav"})
        transcode(fd, plan, out)
    finally:
        os.close(fd)
    facts = measure(out, plan)
    assert facts.sample_rate == 32000
    assert facts.channels == 2
    assert facts.duration_seconds == pytest.approx(0.1, abs=0.05)
    assert facts.size > 0


def test_transcode_mp3_measures_bitrate(tmp_path) -> None:
    src = str(tmp_path / "audio.wav")
    _write_wav(src)
    out = str(tmp_path / "out.mp3")
    fd = _fd(src)
    try:
        plan = resolve_transcode({"format": "mp3", "sample_rate": 44100, "bitrate": 256000})
        transcode(fd, plan, out)
    finally:
        os.close(fd)
    facts = measure(out, plan)
    assert facts.sample_rate == 44100
    assert facts.channels == 2
    assert facts.bitrate > 0
    assert facts.duration_seconds > 0
    assert facts.size > 0


def test_transcode_pcm_is_headerless_measured_from_size(tmp_path) -> None:
    src = str(tmp_path / "audio.wav")
    _write_wav(src)
    out = str(tmp_path / "out.pcm")
    fd = _fd(src)
    try:
        plan = resolve_transcode({"format": "pcm", "sample_rate": 24000})
        transcode(fd, plan, out)
    finally:
        os.close(fd)
    # headerless: no RIFF header, size is a whole number of 4-byte stereo s16 frames
    assert Path(out).read_bytes()[:4] != b"RIFF"
    facts = measure(out, plan)
    assert facts.sample_rate == 24000
    assert facts.channels == 2
    assert facts.size % (NATIVE_CHANNELS * 2) == 0
    assert facts.duration_seconds == pytest.approx(0.1, abs=0.03)


@pytest.mark.parametrize("setting", [{"format": "wav"}, {"format": "mp3", "bitrate": 128000}])
def test_transcode_is_deterministic(tmp_path, setting) -> None:
    src = str(tmp_path / "audio.wav")
    _write_wav(src)
    plan = resolve_transcode(setting)
    outs = []
    for name in ("a", "b"):
        out = str(tmp_path / f"out_{name}.{plan.file_ext}")
        fd = _fd(src)
        try:
            transcode(fd, plan, out)
        finally:
            os.close(fd)
        outs.append(Path(out).read_bytes())
    assert outs[0] == outs[1]  # byte-identical -> hex and url deliveries agree


def test_transcode_of_non_wav_raises(tmp_path) -> None:
    bad = str(tmp_path / "bad.wav")
    Path(bad).write_bytes(b"not audio at all")
    out = str(tmp_path / "out.mp3")
    fd = _fd(bad)
    try:
        with pytest.raises(TranscodeError):
            transcode(fd, resolve_transcode({"format": "mp3"}), out)
    finally:
        os.close(fd)
