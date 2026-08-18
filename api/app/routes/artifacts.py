"""Traversal-safe artifact serving + listing (spec: artifact-serving.md; INV-8, R-17).

A thin router. Every filesystem touch is delegated to ``api/jobs/artifacts.py``: listing via
``scan_listing`` and serving via ``open_member``, which returns an ``O_NOFOLLOW`` file descriptor for a
validated regular file. This module streams that HELD fd — it never re-opens a path — so a symlink swapped
into the artifacts root after validation cannot redirect the response (SEC-1). The artifacts root is read
from ``app.state.config`` (as the jobs router reads its dependencies); ``None`` means persistence is not
configured, so listing is empty and serving 404s. The routes are always mounted so the OpenAPI schema is
independent of whether a host has configured an artifacts directory.
"""

import os
from collections.abc import Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from jobs.artifacts import AUDIO_NAME, JobIdError, open_member, scan_listing

router = APIRouter()

_CHUNK = 65536


def _stream_fd(fd: int) -> Iterator[bytes]:
    """Yield a held fd's bytes in chunks and close it (also on early client disconnect / GC)."""
    with os.fdopen(fd, "rb", closefd=True) as handle:
        while True:
            data = handle.read(_CHUNK)
            if not data:
                break
            yield data


@router.get("/artifacts")
async def list_artifacts(request: Request) -> list[dict]:
    """Newest-first listing of complete artifact directories; empty when nothing is configured/present."""
    root = request.app.state.config.artifacts_dir
    if root is None:
        return []
    return scan_listing(root)


@router.get("/artifacts/{job_id}/{name}")
async def get_artifact(job_id: str, name: str, request: Request) -> StreamingResponse:
    """Serve one canonical member of a completed artifact by streaming an O_NOFOLLOW-validated fd; 404 for
    a crafted id, a non-member name, an incomplete directory, a symlinked member, or an unknown id."""
    root = request.app.state.config.artifacts_dir
    if root is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        opened = open_member(root, job_id, name)
    except JobIdError:
        raise HTTPException(status_code=404, detail="artifact not found") from None
    if opened is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    fd, size = opened
    media_type = "audio/wav" if name == AUDIO_NAME else "application/json"
    return StreamingResponse(
        _stream_fd(fd), media_type=media_type, headers={"Content-Length": str(size)}
    )
