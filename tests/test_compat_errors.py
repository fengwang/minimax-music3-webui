"""base_resp catalogue tests (spec: base-resp-errors.md). CPU-only, no engine."""

from compat.errors import (
    INVALID_PARAMS,
    LOCAL_FAILURE,
    NEVER_EMITTED,
    SUCCESS,
    CompatError,
    base_resp,
    error_envelope,
)


def test_codes_are_the_expected_values() -> None:
    assert (SUCCESS, INVALID_PARAMS, LOCAL_FAILURE) == (0, 2013, 5000)
    # Cloud billing/auth/moderation codes are documented but never emitted (D5).
    assert set(NEVER_EMITTED) == {1002, 1004, 1008, 1026, 2049}


def test_base_resp_shape() -> None:
    assert base_resp(0, "success") == {"status_code": 0, "status_msg": "success"}


def test_error_envelope_is_cloud_shaped_with_null_payload() -> None:
    env = error_envelope(INVALID_PARAMS, "stream: true not supported", trace_id=None)
    assert env["base_resp"] == {"status_code": 2013, "status_msg": "stream: true not supported"}
    assert env["data"] is None
    assert env["extra_info"] is None
    assert env["analysis_info"] is None
    assert env["trace_id"] is None


def test_error_envelope_carries_trace_id_for_local_failure() -> None:
    env = error_envelope(LOCAL_FAILURE, "engine call failed", trace_id="job123")
    assert env["base_resp"]["status_code"] == 5000
    assert env["trace_id"] == "job123"


def test_compat_error_carries_code_and_field() -> None:
    err = CompatError(INVALID_PARAMS, field="stream", message="stream: true not supported")
    assert err.code == 2013
    assert err.field == "stream"
    assert "stream" in str(err)
