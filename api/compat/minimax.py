"""The cloud↔local translation layer — the ONE place the two schemas meet (F6, project_contract §4).

No cloud field name matches a local one, so this module owns the mapping: ``prompt→instructions``,
``lyrics→input``, ``seed``/``max_new_tokens`` pass through, ``stream`` pinned false toward the engine, and
the complete 2013 refusal set (INV-7) — every unhonourable field raised as ``CompatError`` naming the
field, never silently defaulted. It also estimates the hex body for the R-07 pre-dispatch ceiling and
builds the success response envelope.

ACD: every function here is a pure Calculation. The route (Action shell) submits, blocks, transcodes, and
renders; this layer touches no engine, no filesystem, no clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from compat.errors import INVALID_PARAMS, SUCCESS, CompatError, base_resp
from transcode.ffmpeg import TranscodePlan, resolve_transcode

#: The single accepted model id (allow-list of one). Every other documented cloud id and any unknown
#: string is refused through one branch (INV-7); serving Music3 under another id is a silent substitution.
MODEL_ID = "music-3.0"
#: Cloud text caps (F5). Enforced as CHARACTER caps before dispatch (R-20). The 5,000-token prompt limit is
#: recorded as unverified — no tokenizer is available CPU-side — and is NOT enforced as a token count here.
MAX_PROMPT_CHARS = 2000
MAX_LYRICS_CHARS = 3500
#: Additive optional fields (D1). Absent leaves cloud behaviour unchanged: seed 0, a full-length song.
DEFAULT_SEED = 0
DEFAULT_MAX_NEW_TOKENS = 9000
MAX_NEW_TOKENS_CEILING = 9000
_FRAMES_PER_SECOND = 25  # E-08
_BYTES_PER_SAMPLE = 2  # s16le
#: Generous safety ceiling on the estimated ``output_format: hex`` body (R-07). A full-length song stays
#: well under it; it is a belt-and-suspenders net against an absurd request, and ``translate`` accepts an
#: override so the refusal path is demonstrable in a test. ``url`` is the documented alternative.
HEX_MAX_BODY_BYTES = 200 * 1024 * 1024
#: The documented cloud request fields recorded in the sidecar AS RECEIVED (INV-9, A-S4-01). Only the
#: fields the client actually sent are stored, verbatim and JSON-safe; omitted fields are never invented.
#: Cover/refused fields cannot appear in an accepted request, so they never reach this copy.
_RECORDED_CLOUD_FIELDS = (
    "model", "prompt", "lyrics", "stream", "lyrics_optimizer", "is_instrumental",
    "output_format", "seed", "max_new_tokens", "audio_setting",
)


@dataclass(frozen=True)
class LocalRequest:
    """The validated, translated request the route submits, plus what the response/sidecar need.

    ``cloud`` is the received cloud envelope recorded verbatim in the sidecar (INV-9, "as received"): the
    present known fields only, omitted fields not invented. The effective ``seed``/``max_new_tokens`` are
    also recorded in the sidecar's ``request.{seed,max_new_tokens,translated}``, proving they were used and
    not accepted-and-dropped. ``plan`` and ``output_format`` drive delivery.
    """

    input: str
    instructions: str
    seed: int
    max_new_tokens: int
    output_format: str
    plan: TranscodePlan
    cloud: dict[str, object]


def _refuse(field: str | None, message: str) -> CompatError:
    return CompatError(INVALID_PARAMS, field=field, message=message)


def translate(body: object, *, hex_ceiling_bytes: int = HEX_MAX_BODY_BYTES) -> LocalRequest:
    """Pure: validate and translate a cloud request envelope, or raise ``CompatError`` (2013) naming the
    offending field. Validation order is fixed so the FIRST offending field is the one named.
    """
    if not isinstance(body, Mapping):
        raise _refuse(None, "request body must be a JSON object")

    # 1. model allow-list of one (absent or any other value fails here).
    if body.get("model") != MODEL_ID:
        raise _refuse("model", f"model must be {MODEL_ID!r}; got {body.get('model')!r}")

    # 2. boolean flags: only a non-default (truthy) value is refused; a conforming false/absent is a no-op.
    for flag in ("stream", "lyrics_optimizer", "is_instrumental"):
        if body.get(flag):
            raise _refuse(flag, f"{flag} is not supported by the local engine")

    # 3. cover-flow inputs: refused on KEY PRESENCE, any value incl. null/empty (project_contract §5,
    #    "with any value"; these have no documented no-op default, so a present cover field cannot be
    #    silently accepted-and-dropped — INV-7). This differs from the booleans above, which have a
    #    documented false default and so are refused only when truthy.
    for cover in ("cover_feature_id", "audio_url", "audio_base64"):
        if cover in body:
            raise _refuse(cover, f"{cover} (cover flow) is not supported by the local engine")

    # 4. required text, enforced as character caps before dispatch (never truncated).
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= MAX_PROMPT_CHARS:
        raise _refuse("prompt", f"prompt must be a string of 1-{MAX_PROMPT_CHARS} characters")
    lyrics = body.get("lyrics")
    if not isinstance(lyrics, str) or not 1 <= len(lyrics) <= MAX_LYRICS_CHARS:
        raise _refuse("lyrics", f"lyrics must be a string of 1-{MAX_LYRICS_CHARS} characters")

    # 5. delivery + additive fields.
    output_format = body.get("output_format", "hex")
    if output_format not in ("hex", "url"):
        raise _refuse("output_format", "output_format must be 'hex' or 'url'")
    seed = _validate_int(body, "seed", default=DEFAULT_SEED, low=0, high=None)
    max_new_tokens = _validate_int(
        body, "max_new_tokens", default=DEFAULT_MAX_NEW_TOKENS, low=1, high=MAX_NEW_TOKENS_CEILING
    )

    # 6. transcode matrix (R-19): refusals bubble up naming format/sample_rate/bitrate/audio_setting.
    plan = resolve_transcode(body.get("audio_setting"))

    # 7. pre-dispatch hex ceiling (R-07): refuse an over-large hex body before generating anything.
    if output_format == "hex":
        estimate = estimate_hex_bytes(max_new_tokens, plan)
        if estimate > hex_ceiling_bytes:
            raise _refuse(
                "output_format",
                f"hex body ~{estimate} bytes exceeds the {hex_ceiling_bytes}-byte ceiling; use "
                "output_format 'url'",
            )

    # Record the cloud envelope AS RECEIVED (INV-9, A-S4-01): present known fields, verbatim, omitted
    # fields NOT invented. The effective/translated engine params live in the sidecar's
    # request.{seed,max_new_tokens,translated}; a normalized copy is not stored here. The url delivery route
    # re-resolves `audio_setting` from this as-received copy — deterministic, so hex and url still agree.
    cloud = {key: body[key] for key in _RECORDED_CLOUD_FIELDS if key in body}
    return LocalRequest(
        input=lyrics,
        instructions=prompt,
        seed=seed,
        max_new_tokens=max_new_tokens,
        output_format=output_format,
        plan=plan,
        cloud=cloud,
    )


def _validate_int(body: Mapping[str, object], name: str, *, default: int, low: int, high: int | None) -> int:
    """Pure: read an optional integer field, apply ``default`` when absent, and refuse an out-of-range or
    non-integer value (``bool`` is rejected — it is an ``int`` subclass but not a valid numeric input)."""
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        bound = f"{low}..{high}" if high is not None else f">= {low}"
        raise _refuse(name, f"{name} must be an integer {bound}")
    return value


def estimate_hex_bytes(max_new_tokens: int, plan: TranscodePlan) -> int:
    """Pure: an UPPER-BOUND estimate of the hex body for ``output_format: hex`` at ``max_new_tokens``
    frames (25 fps), used only for the pre-dispatch ceiling. The model may stop early, so this over-counts
    — refusing on the upper bound is the safe direction. mp3 uses ``bitrate × duration``; wav/pcm use raw
    PCM size. Hex is two characters per byte."""
    duration = max_new_tokens / _FRAMES_PER_SECOND
    if plan.container == "mp3":
        audio_bytes = plan.bitrate / 8 * duration  # type: ignore[operator]  # mp3 always has a bitrate
    else:
        audio_bytes = duration * plan.sample_rate * plan.channels * _BYTES_PER_SAMPLE
    return int(audio_bytes * 2)


def response_envelope(
    trace_id: str, audio: str, status: int, extra_info: Mapping[str, object]
) -> dict[str, object]:
    """Pure: the full cloud success envelope (F5). ``audio`` is a hex string or a url; ``status`` is 2 for
    a completed blocking generation; ``analysis_info`` is literal null."""
    return {
        "data": {"audio": audio, "status": status},
        "trace_id": trace_id,
        "extra_info": dict(extra_info),
        "analysis_info": None,
        "base_resp": base_resp(SUCCESS, "success"),
    }
