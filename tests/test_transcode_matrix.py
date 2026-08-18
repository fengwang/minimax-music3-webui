"""R-19 combination-rule matrix tests (spec: ffmpeg-transcode.md). Pure resolver; no ffmpeg needed."""

import pytest

from compat.errors import INVALID_PARAMS, CompatError
from transcode.ffmpeg import NATIVE_CHANNELS, NATIVE_SAMPLE_RATE, resolve_transcode


def test_absent_audio_setting_is_native_wav() -> None:
    plan = resolve_transcode(None)
    assert plan.container == "wav"
    assert plan.sample_rate == NATIVE_SAMPLE_RATE == 32000
    assert plan.channels == NATIVE_CHANNELS == 2
    assert plan.bitrate is None


def test_empty_audio_setting_is_native_wav() -> None:
    plan = resolve_transcode({})
    assert plan.container == "wav" and plan.sample_rate == 32000


def test_mp3_honours_rate_and_bitrate() -> None:
    plan = resolve_transcode({"format": "mp3", "sample_rate": 44100, "bitrate": 256000})
    assert (plan.container, plan.sample_rate, plan.bitrate) == ("mp3", 44100, 256000)


def test_mp3_defaults_bitrate_and_rate() -> None:
    plan = resolve_transcode({"format": "mp3"})
    assert (plan.container, plan.sample_rate, plan.bitrate) == ("mp3", 32000, 128000)


@pytest.mark.parametrize("fmt", ["wav", "pcm"])
def test_bitrate_with_wav_or_pcm_is_refused(fmt: str) -> None:
    with pytest.raises(CompatError) as ei:
        resolve_transcode({"format": fmt, "bitrate": 128000})
    assert ei.value.code == INVALID_PARAMS
    assert ei.value.field == "bitrate"


def test_pcm_is_s16le_at_requested_rate() -> None:
    plan = resolve_transcode({"format": "pcm", "sample_rate": 24000})
    assert plan.container == "pcm" and plan.sample_rate == 24000 and plan.bitrate is None


@pytest.mark.parametrize(
    ("setting", "field"),
    [
        ({"format": "flac"}, "format"),
        ({"sample_rate": 48000}, "sample_rate"),
        ({"format": "mp3", "bitrate": 96000}, "bitrate"),
        ({"format": "mp3", "sample_rate": 8000}, "sample_rate"),
    ],
)
def test_out_of_enum_is_refused(setting: dict, field: str) -> None:
    with pytest.raises(CompatError) as ei:
        resolve_transcode(setting)
    assert ei.value.code == INVALID_PARAMS
    assert ei.value.field == field


def test_resample_above_native_is_allowed_plan_records_target() -> None:
    plan = resolve_transcode({"format": "wav", "sample_rate": 44100})
    assert plan.sample_rate == 44100  # resample target; extra_info later reports the delivered rate


def test_audio_setting_wrong_type_is_refused() -> None:
    with pytest.raises(CompatError) as ei:
        resolve_transcode("mp3")  # not an object
    assert ei.value.code == INVALID_PARAMS and ei.value.field == "audio_setting"


@pytest.mark.parametrize(
    ("setting", "field"),
    [
        # CR-2: non-scalar / wrong-type sub-fields must raise a shaped CompatError, never a TypeError.
        ({"format": []}, "format"),
        ({"format": 3}, "format"),
        ({"sample_rate": []}, "sample_rate"),
        ({"sample_rate": "44100"}, "sample_rate"),
        ({"sample_rate": True}, "sample_rate"),  # bool is not a valid int input
        ({"format": "mp3", "bitrate": {}}, "bitrate"),
        ({"format": "mp3", "bitrate": "128000"}, "bitrate"),
        # CR-1: bitrate present (incl. null) with wav/pcm is refused on key presence.
        ({"format": "wav", "bitrate": None}, "bitrate"),
        ({"format": "pcm", "bitrate": None}, "bitrate"),
    ],
)
def test_nonscalar_or_present_null_bitrate_is_refused(setting: dict, field: str) -> None:
    with pytest.raises(CompatError) as ei:
        resolve_transcode(setting)
    assert ei.value.code == INVALID_PARAMS and ei.value.field == field
