"""Local artifact storage: containment, measurement, atomic write, serving, startup validation.

Functional core / imperative shell (ACD):
- Calculations (pure): ``is_within``, ``validate_job_id``, ``measure_wav``, ``build_sidecar``,
  ``deployment_facts_from_env`` — no I/O, testable on any host.
- Actions (filesystem / env): ``resolve_within`` (the single ``realpath``, write-side containment),
  ``validate_artifacts_root``, ``write_artifact``, ``open_member`` / ``scan_listing`` (serve-side).

INV-8: no served path is derived from unsanitised client input. Every request-derived id first passes
``validate_job_id`` (a pure gate that touches no filesystem) BEFORE any path is built. The write side then
resolves the destination with ``resolve_within`` (a ``realpath`` containment check). The serve/list side
opens each request-derived component via ``openat`` with ``O_NOFOLLOW`` from the root directory fd and
asserts a regular file, then streams the HELD fd — so a crafted id is refused early, a symlink escaping the
root is refused by resolution (never by file permissions), and a symlink swapped in after the check cannot
redirect a response (R-17).

INV-9: every artifact has a sidecar whose output facts are MEASURED from the written WAV, never copied
from the model card (the 32 kHz-vs-44100 contradiction, E-17). No database and no TTL reaper (D6).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import wave
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobs.store import JobRecord

#: Strict job-id allow-list. Rejects ``..``, ``/``, absolute paths, a decoded ``%2f``, NUL, dots, and
#: anything over 128 chars. Anchored with ``\A``/``\Z`` (not ``^``/``$``) so a trailing newline cannot
#: slip past. A server-minted ``uuid4().hex`` (32 lowercase hex) matches.
_JOB_ID = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
#: A leading URI scheme (``http://``, ``file://`` …). Rejected outright: an artifacts path is always a
#: local filesystem path, never a URL — the no-remote-fetch gate.
_URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


class JobIdError(ValueError):
    """A job id fails the strict allow-list. Raised BEFORE any path is built (maps to 404/refusal)."""


class UntrustedPathError(ValueError):
    """A resolved path escapes the artifacts root (maps to 404/refusal).

    Carries the offending input and the root for the message, never the resolved escape target — a
    traversal is not echoed back what it pointed at.
    """

    def __init__(self, candidate: str, root: str) -> None:
        super().__init__(f"path {candidate!r} is outside the artifacts root {root!r}")
        self.candidate = candidate
        self.root = root


def validate_job_id(job_id: str) -> str:
    """Pure gate: return ``job_id`` iff it matches the strict allow-list, else raise ``JobIdError``.

    Does no filesystem work, so a crafted id (``..``, an absolute path, a decoded separator, a NUL byte)
    is refused before any path is constructed or any file is opened (INV-8).
    """
    if not isinstance(job_id, str) or _JOB_ID.match(job_id) is None:
        raise JobIdError(f"invalid job id: {job_id!r}")
    return job_id


def is_within(candidate_real: str, root_real: str) -> bool:
    """Pure Calculation: is ``candidate_real`` the root itself or strictly beneath it?

    Separator-aware, so a sibling like ``/srv/artifacts-evil`` does NOT match ``/srv/artifacts`` (no
    partial-component match).
    """
    return candidate_real == root_real or candidate_real.startswith(root_real + os.sep)


def resolve_within(candidate: str, root: str) -> str:
    """Resolve ``candidate`` and return its realpath iff it lies within ``root``; else raise.

    The one Action is ``os.path.realpath`` (collapses ``..``, resolves symlinks). NUL bytes and URL
    schemes are rejected first. The containment test is pure, so containment is host-testable and a
    symlink leaving the root is refused by resolution.
    """
    if "\x00" in candidate or _URL_SCHEME.match(candidate) is not None:
        raise UntrustedPathError(candidate, root)
    candidate_real = os.path.realpath(candidate)
    if is_within(candidate_real, os.path.realpath(root)):
        return candidate_real
    raise UntrustedPathError(candidate, root)


def validate_artifacts_root(root: str) -> str:
    """Action: fail-fast startup check. Return the resolved root iff it exists, is a directory, and is
    writable; else raise ``ValueError`` naming the RESOLVED path so a bad host config aborts startup
    instead of failing at the first generation (deterministic check 6).

    Writability is proven by creating and removing a probe rather than trusting ``os.access`` (which can
    disagree with the real filesystem under root/ACLs/a read-only mount). The probe is a *unique*
    ``mkstemp`` file, created with ``O_CREAT | O_EXCL``: it can never coincide with a pre-planted symlink,
    so a hostile writer to the bind mount cannot make startup truncate an arbitrary target (SEC-2), and
    only the entry this call created is unlinked.
    """
    resolved = os.path.realpath(root)
    if not os.path.exists(resolved):
        raise ValueError(f"artifacts root does not exist: {resolved}")
    if not os.path.isdir(resolved):
        raise ValueError(f"artifacts root is not a directory: {resolved}")
    try:
        fd, probe = tempfile.mkstemp(prefix=".artifacts_write_probe.", dir=resolved)
        os.close(fd)
        os.remove(probe)
    except OSError as exc:
        raise ValueError(f"artifacts root is not writable: {resolved}") from exc
    return resolved


# --------------------------------------------------------------------------------------------------
# Measurement + sidecar (Calculations; INV-9). Output facts are measured from the written WAV so the
# 32 kHz-vs-44100 contradiction inside the model folder (E-17) can never be re-imported into metadata.
# --------------------------------------------------------------------------------------------------

_SIDECAR_VERSION = "1"


@dataclass(frozen=True)
class WavFacts:
    """Facts measured from the emitted audio: WAV header rate/channels/duration plus size and hash."""

    sample_rate: int
    channels: int
    duration_seconds: float
    byte_size: int
    content_hash: str


def measure_wav(audio: bytes) -> WavFacts:
    """Pure Calculation: read the WAV header for rate/channels/duration; hash and size the raw bytes.

    Every parse failure is normalized to ``wave.Error`` — the stdlib ``wave``/``chunk`` parser can raise
    ``EOFError`` (truncated body), ``struct.error`` (malformed header), or a bare ``RuntimeError`` (a chunk
    whose declared size overflows its parent), none of which the single-worker runner catches; left
    un-normalized any of them would escape ``_process`` and wedge the worker. Normalizing here (the same
    "any failure becomes the one documented exception" shape as ``EngineError``) keeps the runner's catch
    correct. A real generation always returns a valid WAV, so a parse failure is a generation failure the
    writer turns into a failed job (never a stored artifact).
    """
    try:
        with wave.open(io.BytesIO(audio), "rb") as reader:
            framerate = reader.getframerate()
            channels = reader.getnchannels()
            nframes = reader.getnframes()
    except Exception as exc:  # normalized to the one documented parse error the runner catches
        raise wave.Error(f"not a parseable WAV: {exc}") from exc
    duration = nframes / framerate if framerate else 0.0
    return WavFacts(
        sample_rate=framerate,
        channels=channels,
        duration_seconds=duration,
        byte_size=len(audio),
        content_hash=hashlib.sha256(audio).hexdigest(),
    )


def deployment_facts_from_env(env: Mapping[str, str]) -> dict:
    """Pure over ``env``: the deployment facts S3 cannot observe. ``version`` gets an honest non-empty
    default (INV-9 wants it non-empty); the pinned image digest and mounted model path are S5's to set."""
    return {
        "version": env.get("MUSIC3_ENGINE_VERSION") or "unpinned-dev",
        "image_digest": env.get("MUSIC3_IMAGE_DIGEST") or None,
        "model_path": env.get("MUSIC3_MODEL_PATH") or None,
    }


def _delta_seconds(start: str | None, end: str | None) -> float | None:
    """Pure: seconds between two ISO instants, or None when either is missing."""
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def build_sidecar(record: JobRecord, deployment: dict, facts: WavFacts, translated: dict) -> dict:
    """Pure Calculation: assemble the INV-9 sidecar from a completed record, deployment facts, the
    measured output facts, and the ``translated`` engine body the runner actually sent.

    ``translated`` is passed in as plain data (not reconstructed here) so this storage module holds no
    engine wire-protocol knowledge — the engine seam stays confined to ``api/engines`` and its caller
    (CR-1, project_contract §4).

    A-S4-01 (additive, INV-9 for S4): a top-level ``trace_id`` (equal to the job id) is always emitted so a
    cloud response can be traced to its artifact (§5); and when the job came through the cloud edge
    (``submission.cloud`` is set) the received envelope is recorded verbatim under ``request.cloud``. A
    native submission leaves ``cloud`` ``None``, so ``request`` is exactly S3's object with no ``cloud`` key.
    """
    request: dict[str, object] = {
        "input": record.submission.input,
        "instructions": record.submission.instructions,
        "seed": record.submission.seed,
        "max_new_tokens": record.submission.max_new_tokens,
        "translated": translated,
    }
    if record.submission.cloud is not None:
        request["cloud"] = dict(record.submission.cloud)
    return {
        "sidecar_version": _SIDECAR_VERSION,
        "job_id": record.id,
        "trace_id": record.id,
        "engine": {
            "name": record.engine,
            "version": deployment["version"],
            "image_digest": deployment["image_digest"],
        },
        "model": {"id": record.model, "path": deployment["model_path"]},
        "request": request,
        "timings": {
            "submitted_at": record.submitted_at,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "generation_seconds": record.generation_seconds,
            "queue_seconds": _delta_seconds(record.submitted_at, record.started_at),
            "total_seconds": _delta_seconds(record.submitted_at, record.ended_at),
        },
        "output": {
            "sample_rate": facts.sample_rate,
            "channels": facts.channels,
            "duration_seconds": facts.duration_seconds,
            "byte_size": facts.byte_size,
            "content_hash": facts.content_hash,
            "content_type": record.content_type,
        },
    }


# --------------------------------------------------------------------------------------------------
# Write protocol (Actions). One exclusive directory per generation, holding exactly the two canonical
# names below. A directory is complete iff BOTH exist; audio is renamed before the sidecar is written,
# so a crash never leaves a sidecar claiming success for a missing audio file.
# --------------------------------------------------------------------------------------------------

AUDIO_NAME = "audio.wav"
SIDECAR_NAME = "sidecar.json"


def _atomic_write_bytes(job_dir: str, name: str, data: bytes) -> None:
    """Action: write ``data`` to ``<job_dir>/<name>`` atomically (temp → flush → fsync → rename).

    ``job_dir`` is a resolve_within-contained path and ``name`` is a fixed canonical constant, so no
    request field is ever concatenated into the opened path (INV-8, static check 5).
    """
    tmp = os.path.join(job_dir, name + ".tmp")
    final = os.path.join(job_dir, name)
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, final)


def _fsync_dir(job_dir: str) -> None:
    """Action: fsync the directory so the renames above are durable across a crash."""
    fd = os.open(job_dir, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_artifact(
    record: JobRecord, root: str, *, translated: dict, env: Mapping[str, str] | None = None
) -> str:
    """Action: write one audio + one sidecar under ``<root>/<job_id>/`` atomically; return the job dir.

    ``translated`` is the engine body the runner sent, passed in as plain data so this module needs no
    engine import (CR-1). Order of operations makes the guarantees testable:
    1. Validate the (server-minted) job id and resolve the destination inside the root — before any I/O.
    2. Measure + build the sidecar first; a non-WAV body raises here, before the directory is created,
       so a failed generation leaves nothing partial behind.
    3. ``os.mkdir`` (exclusive) — a job-id collision is a hard error, never an overwrite.
    4. Rename the audio into place, THEN write and rename the sidecar, THEN fsync the directory — so a
       crash between the two renames leaves an incomplete (unserved, unlisted) directory and no sidecar
       claiming success.

    Raises on any failure; the caller (the runner) turns that into a failed job (never a success).
    """
    if not record.audio:
        raise ValueError("no audio to write")
    env = os.environ if env is None else env
    job_id = validate_job_id(record.id)
    job_dir = resolve_within(os.path.join(root, job_id), root)
    facts = measure_wav(record.audio)  # raises on non-WAV → generation failure, no directory created
    sidecar = build_sidecar(record, deployment_facts_from_env(env), facts, translated)
    os.mkdir(job_dir)  # exclusive: FileExistsError on a job-id collision (adversarial case 3)
    _atomic_write_bytes(job_dir, AUDIO_NAME, record.audio)
    _atomic_write_bytes(job_dir, SIDECAR_NAME, json.dumps(sidecar, indent=2, sort_keys=True).encode())
    _fsync_dir(job_dir)
    return job_dir


# --------------------------------------------------------------------------------------------------
# Serving + listing (Actions; INV-8 serving side). Request-derived directory components are opened via
# ``openat`` with ``O_NOFOLLOW`` from the root directory fd, and members are asserted to be regular
# files. The caller streams the HELD fd — never a re-opened path — so a symlink swapped in after the
# check cannot redirect the response (SEC-1), and an incomplete or symlinked-member directory is neither
# served nor listed (COR-1).
# --------------------------------------------------------------------------------------------------

_SERVABLE = frozenset({AUDIO_NAME, SIDECAR_NAME})


def _open_job_dir_fd(root: str, job_id: str) -> int | None:
    """``openat`` the per-job directory from the (operator-trusted) root with ``O_NOFOLLOW | O_DIRECTORY``
    on the request-derived component, so a symlinked job directory is refused, not followed. Returns the
    dir fd, or ``None`` if missing / not a directory / a symlink. Raises ``JobIdError`` for a crafted id
    BEFORE any filesystem call.
    """
    validate_job_id(job_id)
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        return os.open(job_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError:
        return None
    finally:
        os.close(root_fd)


def _open_regular_member_fd(job_dir_fd: int, name: str) -> tuple[int, int] | None:
    """``openat`` a canonical member with ``O_NOFOLLOW``; return ``(fd, size)`` iff it is a regular file,
    else ``None`` (missing, a symlink → ELOOP, or not a regular file). The returned fd is the exact
    validated inode."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=job_dir_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd, info.st_size


def open_member(root: str, job_id: str, name: str) -> tuple[int, int] | None:
    """TOCTOU-safe accessor: return ``(fd, size)`` for a canonical member, or ``None`` (→ 404).

    Every request-derived component is opened via ``openat`` with ``O_NOFOLLOW`` from the root directory
    fd, and the target is asserted to be a regular file — so no component can be a symlink and the returned
    fd IS the validated inode. The caller streams that held fd without reopening, so a concurrent symlink
    swap between the check and the read cannot redirect the response (SEC-1). Both pair members must open
    safely, so an incomplete or symlinked-member directory is never served (COR-1). Raises ``JobIdError``
    for a crafted id before any filesystem call. The caller owns closing the returned fd.
    """
    if name not in _SERVABLE:
        return None
    job_fd = _open_job_dir_fd(root, job_id)
    if job_fd is None:
        return None
    try:
        audio = _open_regular_member_fd(job_fd, AUDIO_NAME)
        sidecar = _open_regular_member_fd(job_fd, SIDECAR_NAME)
        if audio is None or sidecar is None:  # incomplete or unsafe member → pair is not observable
            for opened in (audio, sidecar):
                if opened is not None:
                    os.close(opened[0])
            return None
        wanted, other = (audio, sidecar) if name == AUDIO_NAME else (sidecar, audio)
        os.close(other[0])
        return wanted
    finally:
        os.close(job_fd)


def _listing_summary(fd: int) -> dict:
    """Parse a sidecar from an already-validated fd (no reopen) and close it; ``{}`` if it will not parse."""
    try:
        with os.fdopen(fd, "rb", closefd=True) as handle:
            sidecar = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {key: sidecar.get(key) for key in ("engine", "model", "output", "timings")}


def scan_listing(root: str) -> list[dict]:
    """Action: newest-first (dir mtime) list of COMPLETE artifact directories with a sidecar summary.

    The filesystem is the index (D6): a single ``scandir`` plus one no-follow open of each member per
    directory, no database and no pagination. A directory is listed only if both canonical members open
    safely as regular files — the SAME ``open_member`` predicate serving uses — so listing never
    advertises an artifact that serving would refuse (COR-1). Symlinked directories are skipped, and the
    sidecar summary is read from the validated fd rather than a re-opened path.
    """
    try:
        scandir_it = os.scandir(root)
    except (FileNotFoundError, NotADirectoryError):
        return []
    candidates: list[tuple[float, str]] = []
    with scandir_it as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                job_id = validate_job_id(entry.name)
            except JobIdError:
                continue
            candidates.append((entry.stat(follow_symlinks=False).st_mtime, job_id))
    candidates.sort(key=lambda item: item[0], reverse=True)
    listing: list[dict] = []
    for mtime, job_id in candidates:
        opened = open_member(root, job_id, SIDECAR_NAME)  # requires BOTH members safe → completeness
        if opened is None:
            continue
        listing.append(
            {
                "job_id": job_id,
                "audio_url": f"/artifacts/{job_id}/{AUDIO_NAME}",
                "sidecar_url": f"/artifacts/{job_id}/{SIDECAR_NAME}",
                "mtime": mtime,
                **_listing_summary(opened[0]),
            }
        )
    return listing
