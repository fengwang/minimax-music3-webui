"""Shared helpers for the S3 generation-loop ui tests (FR-01/02/06).

Not a test module (no ``test_`` prefix), imported by the S3 test files. Extends the S2 stub seam via
``app.state.stub`` without editing ``stub_backend.py``: it only sets values (never new fields), so the
anti-drift fidelity test (``test_stub_fields_equal_the_section4_set``) is unaffected.
"""
from __future__ import annotations

from stub_backend import make_listing_item, make_sidecar


def fill_form(page, *, lyrics: str = "[Verse]\nla la la", caption: str = "Global Metadata: gentle pop") -> None:
    """Fill the two required textareas with valid content (seed + duration keep their defaults)."""
    page.fill("#input", lyrics)
    page.fill("#instructions", caption)


def seed_completed(
    stub,
    n: int,
    *,
    generation_seconds: float = 180.0,
    max_new_tokens: int = 7500,
    duration_seconds: float | None = None,
) -> None:
    """Seed ``n`` completed artifacts (listing + matching sidecars).

    ``generation_seconds`` is the wall-clock time the ETA fit reads (``timings.generation_seconds``);
    ``duration_seconds`` is the delivered audio length (defaults to ``generation_seconds``). Keeping them
    separable lets C3-8 double the generation time while the delivered length and ``max_new_tokens`` stay
    fixed. mtime increases with ``i`` so the newest sample is deterministic.
    """
    if duration_seconds is None:
        duration_seconds = generation_seconds
    artifacts, sidecars = [], {}
    for i in range(n):
        job_id = f"job-{i}"
        item = make_listing_item(job_id, max_new_tokens=max_new_tokens, duration_seconds=duration_seconds)
        item["mtime"] = 1_700_000_000.0 + i
        item["timings"]["generation_seconds"] = generation_seconds
        item["output"]["duration_seconds"] = duration_seconds
        sc = make_sidecar(job_id, max_new_tokens=max_new_tokens, duration_seconds=duration_seconds)
        sc["timings"]["generation_seconds"] = generation_seconds
        artifacts.append(item)
        sidecars[job_id] = sc
    stub.artifacts = artifacts
    stub.sidecars = sidecars


def start_generating(page, stub, *, events=None, seconds: int | None = None) -> None:
    """Fill + submit + drive SSE to the running state. Defaults to a non-terminal ``queued,running``
    sequence so the local elapsed timer keeps ticking (the stream then closes on a heartbeat comment)."""
    stub.events = events if events is not None else [("queued", {}), ("running", {})]
    fill_form(page)
    if seconds is not None:
        page.fill("#max_seconds", str(seconds))
    page.click("#generate")
