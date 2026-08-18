"""The single module that speaks the SGLang-Omni engine's wire protocol.

This is the *only* place in the codebase that knows the engine's HTTP route, request shape, and byte
response (contract project_contract.md §4). Everything else in the app treats generation as "hand an
``EngineRequest`` to an ``EngineClient`` and get an ``EngineResult`` back", so swapping engines (D3
fallback) or filling in the S5 lifecycle changes only this file and ``lifecycle/readiness.py``.

ACD: ``build_body`` is a pure Calculation; ``SglangClient.generate`` is the Action shell (HTTP + clock).
No latency figure is asserted anywhere — only the read-timeout floor is enforced (D9, E-16).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import httpx

# The documented native route and constants (E-04, E-08, E-16).
AUDIO_SPEECH_PATH = "/v1/audio/speech"
ENGINE_NAME = "sglang-omni"
RESPONSE_FORMAT = "wav"
#: Documented ceiling of acoustic frames per request (E-08, E-18). Owned here so the submit boundary and
#: the engine domain share one source of truth.
MAX_NEW_TOKENS_CEILING = 9000
#: A blocking full-length generation needs a long client read timeout; never below this (D9, E-16).
READ_TIMEOUT_FLOOR_SECONDS = 1200.0
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class EngineRequest:
    """The native fields the engine accepts. ``model`` is fixed by config, not chosen per request."""

    model: str
    input: str
    instructions: str
    seed: int
    max_new_tokens: int


@dataclass(frozen=True)
class EngineResult:
    """A completed generation: the raw audio bytes plus provenance S3's sidecar will record (INV-9)."""

    audio: bytes
    content_type: str
    engine: str
    model: str
    generation_seconds: float


class EngineError(RuntimeError):
    """The single failure mode of a generation: non-2xx status, transport failure, or any unexpected
    error — all normalized to this type inside ``SglangClient.generate`` — so callers handle exactly one
    exception. Never returned as success: a returned ``EngineResult`` always carries real bytes.
    """


class EngineClient(Protocol):
    """The seam the job runner depends on. Implemented by ``SglangClient`` in production and by a fake in
    tests, so the whole suite runs with no engine reachable.

    Contract: ``generate`` MUST raise ``EngineError`` (never another exception type) on any failure, so the
    single-slot runner's one ``except EngineError`` frees the slot for every failure and never wedges.
    """

    async def generate(self, request: EngineRequest) -> EngineResult: ...


def build_body(request: EngineRequest) -> dict[str, object]:
    """Build the documented native ``/v1/audio/speech`` JSON body — pure Calculation.

    Sends exactly the documented fields and nothing the engine does not document (E-04, E-08):
    ``stream`` is always ``False`` and ``response_format`` is always ``"wav"``.
    """
    return {
        "model": request.model,
        "input": request.input,
        "instructions": request.instructions,
        "response_format": RESPONSE_FORMAT,
        "seed": request.seed,
        "max_new_tokens": request.max_new_tokens,
        "stream": False,
    }


class SglangClient:
    """Posts the native request to the engine and returns audio bytes plus timing.

    Args:
        base_url: engine base URL (e.g. ``http://sglang:8000``), reached only on the compose network.
        model: the fixed served model id sent in every request.
        read_timeout: desired read timeout in seconds; floored at ``READ_TIMEOUT_FLOOR_SECONDS`` (D9).
        connect_timeout: connection timeout in seconds.
        transport: optional httpx transport, injected by tests; ``None`` uses the real network.

    Side effects: one HTTP POST per ``generate`` call. Raises ``EngineError`` on any non-2xx or transport
    failure. A per-call client is used so cancelling the awaiting task aborts the in-flight request and
    drops the engine connection (the cancel-no-orphan mechanism, INV-1).
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        read_timeout: float = READ_TIMEOUT_FLOOR_SECONDS,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self.read_timeout = max(READ_TIMEOUT_FLOOR_SECONDS, float(read_timeout))
        self._timeout = httpx.Timeout(self.read_timeout, connect=connect_timeout)
        self._transport = transport

    async def generate(self, request: EngineRequest) -> EngineResult:
        body = build_body(request)
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport,
            ) as client:
                response = await client.post(AUDIO_SPEECH_PATH, json=body)
            if response.status_code // 100 != 2:
                raise EngineError(f"engine returned HTTP {response.status_code}")
        except EngineError:
            raise
        except Exception as exc:  # any transport/decode failure becomes the one documented engine error
            raise EngineError(f"engine call failed: {exc}") from exc
        return EngineResult(
            audio=response.content,
            content_type=response.headers.get("content-type", "audio/wav"),
            engine=ENGINE_NAME,
            model=self._model,
            generation_seconds=time.monotonic() - start,
        )
