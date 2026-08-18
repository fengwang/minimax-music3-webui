"""The ``base_resp`` catalogue and error-envelope builders for the cloud edge (spec: base-resp-errors.md).

At ``POST /v1/music_generation`` the only client-facing ``base_resp.status_code`` values are ``0`` (success)
and ``2013`` (invalid params). A local-only failure — engine unavailable, cold-start/readiness timeout,
transcode failure, artifact write failure — uses the single reserved code ``5000`` with an HTTP 5xx status,
never a cloud client-error code (INV-7; the contract forbids teaching a client to retry the unretryable).

ACD: everything here is a pure Calculation (``base_resp``, ``error_envelope``) or immutable Data
(``CompatError``). No I/O, no clock, no engine — host-testable.
"""

from __future__ import annotations

#: Success. ``base_resp.status_code == 0`` with ``status_msg == "success"`` (F5).
SUCCESS = 0
#: Invalid params. Every member of the refusal set returns this, HTTP 400, naming the field (INV-7).
INVALID_PARAMS = 2013
#: The single reserved local failure code (HTTP 5xx). Not a MiniMax cloud code; documented and pinned by a
#: test so it cannot drift. Every local failure uses exactly this value.
LOCAL_FAILURE = 5000
#: Cloud billing/auth/moderation codes: documented as part of the schema, never emitted this phase (D5).
NEVER_EMITTED = (1002, 1004, 1008, 1026, 2049)


class CompatError(Exception):
    """A refusal raised by the pure translation/transcode layer. Carries the ``base_resp`` code and the
    offending field so the route can render an envelope naming it. Refusals are ``INVALID_PARAMS`` (2013);
    the code is explicit rather than assumed so a future edge could reuse the type without a silent default.
    """

    def __init__(self, code: int, *, field: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


def base_resp(code: int, msg: str) -> dict[str, object]:
    """Pure: the ``base_resp`` object exactly as F5 documents it."""
    return {"status_code": code, "status_msg": msg}


def error_envelope(code: int, msg: str, *, trace_id: str | None) -> dict[str, object]:
    """Pure: the full cloud envelope for a failure — schema-shaped with a null payload.

    The shape matches the success envelope (``data``/``trace_id``/``extra_info``/``analysis_info``/
    ``base_resp``) so a cloud client parses errors and successes through the same structure (INV-7); only
    ``base_resp`` and the null payload distinguish a failure. ``trace_id`` is ``None`` for a pre-dispatch
    refusal (no job exists yet) and the job id for a local failure.
    """
    return {
        "data": None,
        "trace_id": trace_id,
        "extra_info": None,
        "analysis_info": None,
        "base_resp": base_resp(code, msg),
    }
