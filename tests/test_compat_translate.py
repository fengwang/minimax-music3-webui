"""Cloud envelope translation + the 2013 refusal set (specs: cloud-compat-envelope, base-resp-errors).

Pure layer tests: no engine, no ffmpeg, no HTTP. The route-level HTTP behaviour is in test_compat_route.py.
"""

import pytest

from compat.errors import INVALID_PARAMS, SUCCESS, CompatError
from compat.minimax import (
    LocalRequest,
    estimate_hex_bytes,
    response_envelope,
    translate,
)

_MIN = {"model": "music-3.0", "prompt": "warm caption", "lyrics": "[Verse]\nhi"}


def test_minimal_request_translates() -> None:
    local = translate(dict(_MIN))
    assert isinstance(local, LocalRequest)
    assert local.instructions == "warm caption"  # prompt -> instructions
    assert local.input == "[Verse]\nhi"  # lyrics -> input
    assert local.seed == 0  # additive default
    assert local.max_new_tokens == 9000  # absent -> full-length default
    assert local.output_format == "hex"  # absent -> hex (cloud default)
    assert local.plan.container == "wav"  # absent audio_setting -> native wav


def test_conforming_defaults_are_accepted_as_noops() -> None:
    local = translate({**_MIN, "stream": False, "lyrics_optimizer": False, "is_instrumental": False})
    assert local.instructions == "warm caption"


def test_seed_and_tokens_forwarded_and_recorded_in_cloud() -> None:
    local = translate({**_MIN, "seed": 7, "max_new_tokens": 250})
    assert (local.seed, local.max_new_tokens) == (7, 250)
    # provably not accepted-and-dropped: the normalized cloud envelope records them for the sidecar
    assert local.cloud["seed"] == 7
    assert local.cloud["max_new_tokens"] == 250


def test_url_output_format_is_honoured() -> None:
    assert translate({**_MIN, "output_format": "url"}).output_format == "url"


def test_cloud_is_recorded_as_received_not_normalized() -> None:
    # CR-3 / INV-9 "as received": supplied fields (incl. a conforming default) are recorded verbatim;
    # omitted fields are NOT invented. Effective values still drive the engine via LocalRequest fields.
    local = translate({**_MIN, "stream": False, "output_format": "url"})
    assert local.cloud["stream"] is False  # a supplied default is recorded as-received
    assert local.cloud["output_format"] == "url"
    assert "seed" not in local.cloud  # omitted -> not invented
    assert "max_new_tokens" not in local.cloud
    assert "audio_setting" not in local.cloud
    assert local.seed == 0 and local.max_new_tokens == 9000  # but the effective values are forwarded


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({**_MIN, "stream": True}, "stream"),
        ({**_MIN, "lyrics_optimizer": True}, "lyrics_optimizer"),
        ({**_MIN, "is_instrumental": True}, "is_instrumental"),
        ({**_MIN, "cover_feature_id": "abc"}, "cover_feature_id"),
        ({**_MIN, "audio_url": "http://x/a.wav"}, "audio_url"),
        ({**_MIN, "audio_base64": "AAAA"}, "audio_base64"),
        ({**_MIN, "model": "music-2.6"}, "model"),
        ({**_MIN, "model": "music-cover"}, "model"),
        ({**_MIN, "model": "music-3.0-free"}, "model"),
        ({**_MIN, "model": "zzz"}, "model"),
        ({"prompt": "c", "lyrics": "l"}, "model"),  # model absent
        ({**_MIN, "output_format": "stream"}, "output_format"),
        ({**_MIN, "max_new_tokens": 9001}, "max_new_tokens"),
        ({**_MIN, "max_new_tokens": 0}, "max_new_tokens"),
        ({**_MIN, "seed": -1}, "seed"),
        ({**_MIN, "prompt": "x" * 2001}, "prompt"),
        ({"model": "music-3.0", "lyrics": "l"}, "prompt"),  # prompt absent
        ({"model": "music-3.0", "prompt": "c"}, "lyrics"),  # lyrics absent
        ({**_MIN, "lyrics": ""}, "lyrics"),
        ({**_MIN, "lyrics": "y" * 3501}, "lyrics"),
        # audio_setting matrix refusals bubble up from resolve_transcode with the field named
        ({**_MIN, "audio_setting": {"format": "wav", "bitrate": 128000}}, "bitrate"),
        ({**_MIN, "audio_setting": {"format": "flac"}}, "format"),
        ({**_MIN, "audio_setting": {"sample_rate": 48000}}, "sample_rate"),
    ],
)
def test_refusal_set_members_raise_2013_naming_the_field(body: dict, field: str) -> None:
    with pytest.raises(CompatError) as ei:
        translate(body)
    assert ei.value.code == INVALID_PARAMS
    assert ei.value.field == field


@pytest.mark.parametrize(
    ("body", "field"),
    [
        # CR-1: cover inputs are refused "with any value" (project_contract §5), incl. present-but-falsey;
        # a present-but-empty/null cover field is a cover-flow input we cannot honour, not a no-op default.
        ({**_MIN, "cover_feature_id": ""}, "cover_feature_id"),
        ({**_MIN, "audio_url": None}, "audio_url"),
        ({**_MIN, "audio_base64": []}, "audio_base64"),
        # CR-1: bitrate is refused for wav/pcm on key presence, incl. an explicit null.
        ({**_MIN, "audio_setting": {"format": "wav", "bitrate": None}}, "bitrate"),
        ({**_MIN, "audio_setting": {"format": "pcm", "bitrate": None}}, "bitrate"),
        # CR-2: non-scalar enum values must become a shaped 2013, never a TypeError/HTTP 500.
        ({**_MIN, "audio_setting": {"format": []}}, "format"),
        ({**_MIN, "audio_setting": {"sample_rate": []}}, "sample_rate"),
        ({**_MIN, "audio_setting": {"format": "mp3", "bitrate": {}}}, "bitrate"),
        ({**_MIN, "audio_setting": {"sample_rate": "44100"}}, "sample_rate"),  # string, not int
    ],
)
def test_falsey_and_nonscalar_inputs_are_refused_2013(body: dict, field: str) -> None:
    with pytest.raises(CompatError) as ei:
        translate(body)
    assert ei.value.code == INVALID_PARAMS
    assert ei.value.field == field


def test_non_object_body_is_refused() -> None:
    with pytest.raises(CompatError) as ei:
        translate("not an object")
    assert ei.value.code == INVALID_PARAMS


def test_over_length_prompt_is_refused_not_truncated() -> None:
    with pytest.raises(CompatError):
        translate({**_MIN, "prompt": "x" * 2001})
    # boundary: exactly 2000 is accepted
    assert translate({**_MIN, "prompt": "x" * 2000}).instructions == "x" * 2000


def test_hex_ceiling_refuses_before_dispatch_when_estimate_exceeds() -> None:
    # inject a tiny ceiling to demonstrate the pre-dispatch refusal (R-07, adversarial case 3)
    with pytest.raises(CompatError) as ei:
        translate({**_MIN, "output_format": "hex", "max_new_tokens": 9000}, hex_ceiling_bytes=1024)
    assert ei.value.code == INVALID_PARAMS
    assert ei.value.field == "output_format"
    # url is not gated by the hex ceiling
    assert translate({**_MIN, "output_format": "url", "max_new_tokens": 9000}, hex_ceiling_bytes=1024)


def test_estimate_hex_bytes_scales_with_frames_and_format() -> None:
    from transcode.ffmpeg import resolve_transcode

    wav = resolve_transcode({"format": "wav", "sample_rate": 44100})
    mp3 = resolve_transcode({"format": "mp3", "bitrate": 128000})
    # 9000 frames = 360 s; wav is uncompressed (2 bytes/sample/ch), mp3 is far smaller; hex doubles both
    assert estimate_hex_bytes(9000, wav) == 360 * 44100 * 2 * 2 * 2
    assert estimate_hex_bytes(9000, mp3) < estimate_hex_bytes(9000, wav)


def test_response_envelope_shape() -> None:
    extra = {"music_duration": 10.0, "music_sample_rate": 32000, "music_channel": 2,
             "bitrate": 1024000, "music_size": 640000}
    env = response_envelope("job42", "deadbeef", 2, extra)
    assert env["data"] == {"audio": "deadbeef", "status": 2}
    assert env["trace_id"] == "job42"
    assert env["extra_info"] == extra
    assert env["analysis_info"] is None
    assert env["base_resp"] == {"status_code": SUCCESS, "status_msg": "success"}
