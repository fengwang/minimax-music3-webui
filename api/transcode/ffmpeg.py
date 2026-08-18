"""ffmpeg post-processing: the R-19 combination-rule matrix (pure) and the transcode/measure Actions.

The engine returns one format only — ``pcm_s16le, 32000 Hz, 2 ch, 16-bit`` WAV (S1, R-08). This module
turns that WAV into the requested ``audio_setting`` and measures the delivered file so ``extra_info`` is
measured, never assumed (E-17). ``resolve_transcode`` and ``extra_info`` are pure Calculations; ``transcode``
and ``measure`` are Actions over a real ``ffmpeg``/``ffprobe``.

INV-8: ``transcode`` reads the WAV through the O_NOFOLLOW fd handed back by ``jobs.artifacts.open_member``
via ``/proc/self/fd/<fd>`` (the fd is passed to the child with ``pass_fds``), so it never rebuilds a path
from client input and reads the exact validated inode.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from compat.errors import INVALID_PARAMS, CompatError

# Native output facts, settled by S1 (R-08). The only audio constants S4 hard-codes.
NATIVE_SAMPLE_RATE = 32000
NATIVE_CHANNELS = 2
_BYTES_PER_SAMPLE = 2  # s16le

#: Documented cloud enums (F5). ``sample_rate`` above native is resampled, never advertised as higher
#: fidelity — the delivered rate is reported and the sidecar records both requested and native.
SAMPLE_RATE_ENUM = frozenset({16000, 24000, 32000, 44100})
BITRATE_ENUM = frozenset({32000, 64000, 128000, 256000})
FORMAT_ENUM = frozenset({"mp3", "wav", "pcm"})
#: mp3 with no explicit bitrate takes a documented default (a member of the enum).
DEFAULT_MP3_BITRATE = 128000


class TranscodeError(RuntimeError):
    """The single failure mode of a transcode/measure Action (non-zero exit, tool missing, bad output).

    Normalized here the same way ``EngineError`` is in the engine seam, so the route maps any transcode
    failure to the one reserved local code 5000 (never a client 2013).
    """


@dataclass(frozen=True)
class TranscodePlan:
    """A resolved, valid transcode target. Immutable Data; carries everything the Actions need."""

    container: str  # "mp3" | "wav" | "pcm"
    sample_rate: int
    channels: int
    bitrate: int | None  # mp3 only; None for wav/pcm

    @property
    def media_type(self) -> str:
        return {"mp3": "audio/mpeg", "wav": "audio/wav", "pcm": "application/octet-stream"}[self.container]

    @property
    def file_ext(self) -> str:
        return {"mp3": "mp3", "wav": "wav", "pcm": "pcm"}[self.container]


def _refuse(field: str, message: str) -> CompatError:
    return CompatError(INVALID_PARAMS, field=field, message=message)


def _is_int(value: object) -> bool:
    """True iff ``value`` is a real integer — ``bool`` is an ``int`` subclass but not a valid numeric input."""
    return isinstance(value, int) and not isinstance(value, bool)


def resolve_transcode(audio_setting: Mapping[str, object] | None) -> TranscodePlan:
    """Pure: resolve ``audio_setting`` to a valid ``TranscodePlan`` or raise ``CompatError`` (2013) naming
    the offending field (R-19 matrix). Absent/empty settings deliver the native WAV.

    Rules: ``format`` defaults ``wav``; ``sample_rate`` defaults native; for ``mp3`` ``bitrate`` defaults
    128000; for ``wav``/``pcm`` a ``bitrate`` supplied by key presence (an explicit ``null`` counts) is
    refused (meaningless — stricter than the cloud spec, a recorded divergence). Every supplied value is
    type-checked BEFORE enum membership, so a non-scalar (list/dict/str/bool) becomes a shaped 2013 rather
    than a ``TypeError`` → HTTP 500 (CR-2).
    """
    if audio_setting is None:
        audio_setting = {}
    if not isinstance(audio_setting, Mapping):
        raise _refuse("audio_setting", "audio_setting must be an object")

    fmt = audio_setting.get("format", "wav")
    if not isinstance(fmt, str) or fmt not in FORMAT_ENUM:
        raise _refuse("format", f"audio_setting.format must be one of {sorted(FORMAT_ENUM)}; got {fmt!r}")

    sample_rate = audio_setting.get("sample_rate", NATIVE_SAMPLE_RATE)
    if not _is_int(sample_rate) or sample_rate not in SAMPLE_RATE_ENUM:
        raise _refuse(
            "sample_rate",
            f"audio_setting.sample_rate must be one of {sorted(SAMPLE_RATE_ENUM)}; got {sample_rate!r}",
        )

    if fmt in ("wav", "pcm"):
        if "bitrate" in audio_setting:  # presence (incl. null) is refused for wav/pcm (CR-1, R-19)
            raise _refuse("bitrate", f"bitrate is meaningless with format {fmt!r} and is refused")
        bitrate = None
    else:  # mp3
        bitrate = audio_setting.get("bitrate", DEFAULT_MP3_BITRATE)
        if not _is_int(bitrate) or bitrate not in BITRATE_ENUM:
            raise _refuse(
                "bitrate", f"audio_setting.bitrate must be one of {sorted(BITRATE_ENUM)}; got {bitrate!r}"
            )

    return TranscodePlan(
        container=fmt, sample_rate=int(sample_rate), channels=NATIVE_CHANNELS, bitrate=bitrate
    )


def _output_args(plan: TranscodePlan) -> list[str]:
    """Pure: the ffmpeg output arguments for a plan. ``-map_metadata -1`` drops input metadata so the
    output is a deterministic function of (input, plan, ffmpeg build) — the byte-parity guarantee."""
    common = ["-map_metadata", "-1", "-ar", str(plan.sample_rate), "-ac", str(plan.channels)]
    if plan.container == "mp3":
        return [*common, "-c:a", "libmp3lame", "-b:a", str(plan.bitrate), "-f", "mp3"]
    if plan.container == "wav":
        return [*common, "-c:a", "pcm_s16le", "-f", "wav"]
    return [*common, "-c:a", "pcm_s16le", "-f", "s16le"]  # pcm: headerless raw s16le


def transcode(input_fd: int, plan: TranscodePlan, out_path: str) -> None:
    """Action: transcode the WAV behind ``input_fd`` to ``out_path`` per ``plan`` via a real ffmpeg.

    Reads the validated O_NOFOLLOW fd through ``/proc/self/fd/<fd>`` (the child inherits it via
    ``pass_fds``), so no path is rebuilt from client input (INV-8) and the whole WAV is never loaded into
    Python. Raises ``TranscodeError`` on any non-zero exit or a missing ffmpeg — the caller maps that to
    the reserved local code 5000.
    """
    os.lseek(input_fd, 0, os.SEEK_SET)  # inherited fd shares this offset with the child
    args = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-i", f"/proc/self/fd/{input_fd}", *_output_args(plan), out_path,
    ]
    try:
        proc = subprocess.run(args, pass_fds=(input_fd,), capture_output=True, check=False)
    except FileNotFoundError as exc:  # ffmpeg not installed
        raise TranscodeError("ffmpeg not found") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace")[:500]
        raise TranscodeError(f"ffmpeg exited {proc.returncode}: {detail}")


@dataclass(frozen=True)
class AudioFacts:
    """Facts measured from the delivered file: the source of ``extra_info`` (INV-9)."""

    duration_seconds: float
    sample_rate: int
    channels: int
    bitrate: int
    size: int


def measure(out_path: str, plan: TranscodePlan) -> AudioFacts:
    """Action: measure the delivered file for ``extra_info``.

    mp3/wav carry a header, so ``ffprobe`` reads rate/channels/duration/bitrate directly. Headerless pcm
    has nothing to probe, so its facts are derived from the measured byte size and the plan's known rate and
    channels (documented: for raw pcm the rate is definitional, not embedded). ``size`` is always ``stat``.
    """
    size = os.stat(out_path).st_size
    if plan.container == "pcm":
        frame_bytes = plan.sample_rate * plan.channels * _BYTES_PER_SAMPLE
        duration = size / frame_bytes if frame_bytes else 0.0
        return AudioFacts(duration, plan.sample_rate, plan.channels, plan.sample_rate * plan.channels * 16, size)
    info = _ffprobe(out_path)
    stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = info.get("format", {})
    sample_rate = int(stream.get("sample_rate") or plan.sample_rate)
    channels = int(stream.get("channels") or plan.channels)
    duration = float(fmt.get("duration") or stream.get("duration") or 0.0)
    bitrate = int(fmt.get("bit_rate") or stream.get("bit_rate") or 0) or sample_rate * channels * 16
    return AudioFacts(duration, sample_rate, channels, bitrate, size)


def _ffprobe(path: str) -> dict:
    args = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path]
    try:
        proc = subprocess.run(args, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise TranscodeError("ffprobe not found") from exc
    if proc.returncode != 0:
        raise TranscodeError(f"ffprobe exited {proc.returncode}")
    try:
        return json.loads(proc.stdout or b"{}")
    except ValueError as exc:
        raise TranscodeError(f"ffprobe emitted unparseable JSON: {exc}") from exc


def extra_info(facts: AudioFacts) -> dict[str, object]:
    """Pure: map measured ``AudioFacts`` to the cloud ``extra_info`` object (F5)."""
    return {
        "music_duration": facts.duration_seconds,
        "music_sample_rate": facts.sample_rate,
        "music_channel": facts.channels,
        "bitrate": facts.bitrate,
        "music_size": facts.size,
    }
