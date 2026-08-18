"""The cloud-compatible edge: ``POST /v1/music_generation`` and its ``url`` delivery route.

This is the Action shell (project_contract §4). It translates the cloud envelope (pure, in
``compat.minimax``), submits ONE job to the same single-slot runner the WebUI uses (INV-1), blocks until
the job is terminal, then delivers the requested representation on top of S3's one stored ``audio.wav``
(delivery-model B): ``hex`` streams the transcoded bytes inline; ``url`` links a GET route that re-runs the
SAME deterministic transcode from ``audio.wav`` per the ``audio_setting`` recorded in the sidecar. No second
audio file is ever stored and no second write path is added — the artifact read goes through S3's
traversal-safe ``open_member`` (INV-8).

Refusals return base_resp 2013 (HTTP 400) naming the field; local failures return the reserved code 5000
(HTTP 5xx), never a cloud client-error code (INV-7).
"""

from __future__ import annotations

import asyncio
import binascii
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from compat.errors import INVALID_PARAMS, LOCAL_FAILURE, CompatError, error_envelope
from compat.minimax import response_envelope, translate
from jobs.artifacts import AUDIO_NAME, SIDECAR_NAME, JobIdError, open_member
from jobs.store import JobStatus, Submission
from transcode.ffmpeg import TranscodeError, TranscodePlan, extra_info, measure, resolve_transcode, transcode

router = APIRouter()
_log = logging.getLogger(__name__)

_POLL_SECONDS = 0.05
_CHUNK = 65536
_TERMINAL = frozenset({JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled})
#: A unique token placed in the audio field so the hex JSON can be assembled around a streamed body without
#: ever materialising the whole hex string (R-07). Plain ASCII, so ``json.dumps`` never rewrites it, and it
#: appears exactly once (trace ids are hex, other fields are numbers/fixed strings).
_AUDIO_PLACEHOLDER = "S4HEXAUDIOPLACEHOLDER"
#: SS-1: hard cap on the request body read before JSON parsing, so an unauthenticated client (D5) cannot
#: make the edge buffer an unbounded body before ``translate`` can reject it. A valid cloud request is a
#: few KB (prompt <= 2000, lyrics <= 3500 characters); 1 MiB is generous and rejects abusive bodies.
MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024
#: TP-1: the app-scoped bound on concurrent delivery transcodes (see ``_delivery_slot``).
MAX_CONCURRENT_DELIVERIES = 2


# --------------------------------------------------------------------------------------------------
# OpenAPI documentation models (AR-1). These describe the public cloud contract so a client / codegen can
# discover the exact request, success, and error shapes. The route returns hand-built dicts (JSONResponse),
# so these models are documentation-only — they are not used to validate the runtime body/response.
# --------------------------------------------------------------------------------------------------


class BaseResp(BaseModel):
    """The MiniMax ``base_resp`` object. At this edge ``status_code`` is 0 (success), 2013 (invalid params),
    or the reserved local-failure code 5000."""

    status_code: int = Field(examples=[0, 2013, 5000])
    status_msg: str


class ExtraInfo(BaseModel):
    """Measured from the delivered file (never assumed)."""

    music_duration: float
    music_sample_rate: int
    music_channel: int
    bitrate: int
    music_size: int


class MusicData(BaseModel):
    audio: str = Field(description="hex-encoded audio, or a URL to GET /v1/music_generation/result/{trace_id}")
    status: int = Field(description="2 = completed", examples=[2])


class MusicGenerationResponse(BaseModel):
    data: MusicData
    trace_id: str
    extra_info: ExtraInfo
    analysis_info: None = None
    base_resp: BaseResp


class ErrorEnvelope(BaseModel):
    """A refusal (HTTP 400, code 2013) or a local failure (HTTP 5xx, code 5000): the cloud envelope shape
    with a null payload."""

    data: None = None
    trace_id: str | None = None
    extra_info: None = None
    analysis_info: None = None
    base_resp: BaseResp


#: Hand-authored request schema for ``openapi_extra`` — inline (no ``$ref``/``$defs``) so it resolves at the
#: OpenAPI document root. The route parses the raw body itself (to return 2013, not FastAPI's 422), so this
#: documents the contract without FastAPI validating against it.
_REQUEST_OPENAPI_SCHEMA = {
    "type": "object",
    "required": ["model", "prompt", "lyrics"],
    "properties": {
        "model": {"type": "string", "enum": ["music-3.0"], "description": "only music-3.0 is honoured"},
        "prompt": {"type": "string", "maxLength": 2000, "description": "music caption -> engine instructions"},
        "lyrics": {"type": "string", "minLength": 1, "maxLength": 3500, "description": "lyrics -> engine input"},
        "stream": {"type": "boolean", "default": False, "description": "must be false; true is refused (2013)"},
        "output_format": {"type": "string", "enum": ["hex", "url"], "default": "hex"},
        "audio_setting": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["mp3", "wav", "pcm"]},
                "sample_rate": {"type": "integer", "enum": [16000, 24000, 32000, 44100]},
                "bitrate": {"type": "integer", "enum": [32000, 64000, 128000, 256000], "description": "mp3 only"},
            },
        },
        "seed": {"type": "integer", "minimum": 0, "default": 0, "description": "additive; forwarded to the engine"},
        "max_new_tokens": {
            "type": "integer", "minimum": 1, "maximum": 9000, "default": 9000,
            "description": "additive; acoustic frames at 25 fps",
        },
    },
}
_POST_RESPONSES = {
    200: {"model": MusicGenerationResponse, "description": "Completed generation; base_resp.status_code 0."},
    400: {"model": ErrorEnvelope, "description": "Refused unhonourable field; base_resp.status_code 2013."},
    503: {"model": ErrorEnvelope, "description": "Local failure (engine/transcode/write); base_resp.status_code 5000."},
}


@router.post(
    "/v1/music_generation",
    responses=_POST_RESPONSES,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _REQUEST_OPENAPI_SCHEMA}},
        }
    },
)
async def music_generation(request: Request) -> Response:
    """Blocking cloud-compatible music generation (INV-7).

    Accepts the MiniMax cloud request envelope, translates it to the local engine, runs one generation on
    the single slot (INV-1), and returns the cloud response envelope. Every field the local engine cannot
    honour is refused with base_resp code 2013 (HTTP 400) naming the field, never silently defaulted. A
    local-only failure (engine unavailable, cold-start/readiness timeout, transcode failure, artifact write
    failure) returns the reserved base_resp code 5000 with an HTTP 5xx status, never a cloud client-error
    code, so an integrating client is not taught to retry a request that can never succeed.

    Because generation is blocking, a client MUST set a read timeout of at least 1200 seconds on an idle
    system (F13); with concurrency one a queued caller also waits out any render ahead of it plus a possible
    cold start. output_format hex returns the audio inline as a large single JSON body; output_format url
    returns a link to GET /v1/music_generation/result/{trace_id}. The WebUI never calls this route (INV-12).
    """
    try:
        body = await _read_json_body(request)
        local = translate(body)
    except CompatError as exc:
        return _refused(exc)

    submission = Submission(
        input=local.input,
        instructions=local.instructions,
        seed=local.seed,
        max_new_tokens=local.max_new_tokens,
        cloud=local.cloud,
    )
    record = await request.app.state.runner.submit(submission)
    trace_id = record.id
    status = await _await_terminal(request.app.state.store, trace_id)
    if status is not JobStatus.succeeded:
        error = request.app.state.store.get(trace_id).error or f"job {status.value}"
        return _local_failure(trace_id, error)

    root = request.app.state.config.artifacts_dir
    if root is None:
        return _local_failure(trace_id, "artifacts directory is not configured")
    opened = _open_or_none(root, trace_id, AUDIO_NAME)
    if opened is None:
        return _local_failure(trace_id, "generated artifact not found on disk")
    fd, _size = opened
    tmp: str | None = None
    try:
        # Both ffmpeg subprocesses run off the event loop so SSE/health stay responsive; any transcode,
        # measure, or temp-file OSError becomes the reserved local failure (never an unmapped 500). The
        # delivery slot bounds concurrent transcodes (TP-1) — harmless here since POST is already serial.
        async with _delivery_slot(request):
            tmp = await _transcode_to_temp(fd, local.plan)
            info = extra_info(await asyncio.to_thread(measure, tmp, local.plan))
    except (TranscodeError, OSError) as exc:
        if tmp is not None:
            _safe_unlink(tmp)
        _log.warning("job %s: delivery post-processing failed: %s", trace_id, exc)
        return _local_failure(trace_id, "audio post-processing failed")

    if local.output_format == "url":
        _safe_unlink(tmp)
        return JSONResponse(
            response_envelope(trace_id, _result_url(request, trace_id), 2, info), status_code=200
        )
    return StreamingResponse(_hex_body(tmp, trace_id, info), media_type="application/json")


@router.get(
    "/v1/music_generation/result/{trace_id}",
    responses={
        200: {
            "content": {"audio/mpeg": {}, "audio/wav": {}, "application/octet-stream": {}},
            "description": "Transcoded audio bytes for output_format url.",
        },
        404: {"description": "Unknown, incomplete, or non-cloud artifact."},
        503: {"model": ErrorEnvelope, "description": "Local delivery failure; base_resp.status_code 5000."},
    },
)
async def music_result(trace_id: str, request: Request) -> Response:
    """Deliver the audio for ``output_format: url``, transcoded on demand from the one stored ``audio.wav``
    per the ``audio_setting`` recorded in that job's sidecar (delivery-model B). Traversal-safe: the id is
    validated and both members are opened via ``open_member`` (INV-8). 404 for a crafted, missing,
    incomplete, or non-cloud artifact."""
    root = request.app.state.config.artifacts_dir
    if root is None:
        raise HTTPException(status_code=404, detail="not found")
    audio_setting = _recorded_audio_setting(root, trace_id)  # 404 for missing/native/unparseable
    try:
        plan = resolve_transcode(audio_setting)
    except CompatError as exc:
        # a parseable-but-invalid recorded setting is stored-artifact corruption, a local failure — it uses
        # the reserved 5000 envelope like the other delivery failures, not a bare 500 (CR-4).
        _log.warning("result %s: recorded audio_setting is invalid: %s", trace_id, exc)
        return _local_failure(trace_id, "recorded delivery settings are invalid")
    opened = _open_or_none(root, trace_id, AUDIO_NAME)
    if opened is None:
        raise HTTPException(status_code=404, detail="not found")
    fd, _size = opened
    try:
        async with _delivery_slot(request):  # bound concurrent delivery transcodes (TP-1)
            tmp = await _transcode_to_temp(fd, plan)
    except (TranscodeError, OSError) as exc:
        # A delivery transcode failure is a local failure, so it uses the reserved code 5000 envelope
        # (HTTP 5xx), never a bare error — matching the POST edge (spec: ffmpeg-transcode.md). Near
        # unreachable: the identical transcode already succeeded at POST, so this needs the stored WAV to
        # have changed underneath us.
        _log.warning("result %s: delivery transcode failed: %s", trace_id, exc)
        return _local_failure(trace_id, "delivery transcode failed")
    headers = {"Content-Length": str(os.path.getsize(tmp))}
    return StreamingResponse(_file_body(tmp), media_type=plan.media_type, headers=headers)


# --------------------------------------------------------------------------------------------------
# Helpers. The pure envelope/plan work lives in compat/transcode; these are the thin I/O glue.
# --------------------------------------------------------------------------------------------------


async def _read_json_body(request: Request) -> object:
    """Read and JSON-parse the request body with a hard size cap (SS-1). Rejects an over-limit
    ``Content-Length`` up front and caps the streamed bytes, so an unauthenticated client cannot make the
    edge buffer an unbounded body before ``translate`` can reject it. Raises ``CompatError`` (2013) on an
    over-limit body or invalid JSON, so the caller renders the cloud-shaped refusal."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_REQUEST_BODY_BYTES:
        raise CompatError(INVALID_PARAMS, field=None, message=f"request body exceeds {MAX_REQUEST_BODY_BYTES} bytes")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_REQUEST_BODY_BYTES:
            raise CompatError(
                INVALID_PARAMS, field=None, message=f"request body exceeds {MAX_REQUEST_BODY_BYTES} bytes"
            )
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks) or b"null")
    except ValueError as exc:
        raise CompatError(INVALID_PARAMS, field=None, message="request body must be valid JSON") from exc


def _delivery_slot(request: Request):
    """The app-scoped bound on concurrent delivery transcodes (TP-1), or an unbounded no-op if the app did
    not wire one. Bounding here rather than in the runner is safe — POST transcodes are already serialised
    by the single slot (INV-1); this caps a burst of concurrent ``url`` GET deliveries so N full-length
    ffmpegs cannot exhaust CPU/temp storage on the unauthenticated edge (R-09)."""
    return getattr(request.app.state, "delivery_slots", None) or contextlib.nullcontext()


async def _await_terminal(store, job_id: str) -> JobStatus:
    """Block until the job is terminal, yielding the event loop between polls. No route-level timeout: the
    engine's own read timeout and bounded readiness guarantee the job terminates (then this returns)."""
    while True:
        status = store.get(job_id).status
        if status in _TERMINAL:
            return status
        await asyncio.sleep(_POLL_SECONDS)


def _refused(exc: CompatError) -> JSONResponse:
    """A 2013 refusal: HTTP 400 with the cloud envelope; the message already names the offending field."""
    return JSONResponse(error_envelope(exc.code, exc.message, trace_id=None), status_code=400)


def _local_failure(trace_id: str, message: str) -> JSONResponse:
    """A local-only failure: HTTP 5xx with the single reserved code 5000 (never a cloud client-error code)."""
    return JSONResponse(error_envelope(LOCAL_FAILURE, message, trace_id=trace_id), status_code=503)


def _result_url(request: Request, trace_id: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/v1/music_generation/result/{trace_id}"


def _open_or_none(root: str, job_id: str, name: str) -> tuple[int, int] | None:
    """``open_member`` but a crafted id becomes ``None`` (→ 404/local-failure) rather than an exception."""
    try:
        return open_member(root, job_id, name)
    except JobIdError:
        return None


def _recorded_audio_setting(root: str, trace_id: str) -> object:
    """Return the ``audio_setting`` the POST recorded in the sidecar (as received; may be ``None`` when the
    client omitted it → native wav). 404 for a crafted/missing id, an unparseable sidecar, or a native
    (non-cloud) artifact. The caller resolves it, so an invalid recorded setting maps to a 5000 envelope
    (CR-4) rather than a 404."""
    opened = _open_or_none(root, trace_id, SIDECAR_NAME)
    if opened is None:
        raise HTTPException(status_code=404, detail="not found")
    fd, _size = opened
    try:
        with os.fdopen(fd, "rb", closefd=True) as handle:
            sidecar = json.load(handle)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    request_obj = sidecar.get("request")
    cloud = request_obj.get("cloud") if isinstance(request_obj, dict) else None
    if not isinstance(cloud, dict):
        # native artifact (no cloud envelope) or a corrupt sidecar → not deliverable by this route
        raise HTTPException(status_code=404, detail="not found")
    return cloud.get("audio_setting")


async def _transcode_to_temp(fd: int, plan: TranscodePlan) -> str:
    """Transcode the WAV behind ``fd`` to a fresh temp file and return its path; always closes ``fd``. Runs
    the blocking ffmpeg subprocess off the event loop so SSE/health/listing stay responsive."""

    def work() -> str:
        try:
            tmp_fd, tmp = tempfile.mkstemp(prefix="s4_deliver_", suffix="." + plan.file_ext)
            os.close(tmp_fd)
            try:
                transcode(fd, plan, tmp)
            except BaseException:
                _safe_unlink(tmp)
                raise
            return tmp
        finally:
            os.close(fd)  # the input fd is always released, even if mkstemp/transcode fails

    return await asyncio.to_thread(work)


def _hex_body(tmp_path: str, trace_id: str, info: dict) -> Iterator[bytes]:
    """Stream the cloud envelope with the audio hex read from ``tmp_path`` in bounded chunks — the whole hex
    string is never materialised (R-07). The envelope shape is the SAME builder ``url`` uses, split around a
    placeholder, so hex and url envelopes agree. The temp file is removed when streaming ends (also on a
    client disconnect: the generator's ``finally`` runs on ``GeneratorExit``)."""
    template = response_envelope(trace_id, _AUDIO_PLACEHOLDER, 2, info)
    prefix, suffix = json.dumps(template).split(_AUDIO_PLACEHOLDER)
    try:
        yield prefix.encode()
        with open(tmp_path, "rb") as handle:
            while chunk := handle.read(_CHUNK):
                yield binascii.hexlify(chunk)
        yield suffix.encode()
    finally:
        _safe_unlink(tmp_path)


def _file_body(tmp_path: str) -> Iterator[bytes]:
    """Stream a temp file in bounded chunks and remove it afterwards (also on client disconnect)."""
    try:
        with open(tmp_path, "rb") as handle:
            while chunk := handle.read(_CHUNK):
                yield chunk
    finally:
        _safe_unlink(tmp_path)


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
