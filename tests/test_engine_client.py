"""Engine client seam tests (spec: engine-client.md). CPU-only, no engine reachable."""

import httpx
import pytest

from engines.sglang.client import (
    MAX_NEW_TOKENS_CEILING,
    EngineError,
    EngineRequest,
    SglangClient,
    build_body,
)

_REQ = EngineRequest(
    model="MiniMaxAI/MiniMax-Music3",
    input="[Verse]\nhello",
    instructions="a warm caption",
    seed=0,
    max_new_tokens=250,
)

def test_build_body_is_native_and_exhaustive() -> None:
    # Exact key-set equality proves no undocumented/cloud parameter can leak into the native request.
    body = build_body(_REQ)
    assert set(body) == {
        "model", "input", "instructions", "response_format", "seed", "max_new_tokens", "stream",
    }
    assert body["response_format"] == "wav"
    assert body["stream"] is False
    assert body["model"] == "MiniMaxAI/MiniMax-Music3"
    assert body["input"] == "[Verse]\nhello"
    assert body["instructions"] == "a warm caption"
    assert body["seed"] == 0
    assert body["max_new_tokens"] == 250


def test_ceiling_constant_is_9000() -> None:
    assert MAX_NEW_TOKENS_CEILING == 9000


def test_read_timeout_floored_at_1200() -> None:
    assert SglangClient(base_url="http://sglang:8000", model="m", read_timeout=5.0).read_timeout >= 1200
    assert SglangClient(base_url="http://sglang:8000", model="m", read_timeout=1500.0).read_timeout == 1500.0


async def test_generate_returns_bytes_and_timing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/speech"
        return httpx.Response(200, content=b"RIFF....WAVEfmt ", headers={"content-type": "audio/wav"})

    client = SglangClient(
        base_url="http://sglang:8000", model="MiniMaxAI/MiniMax-Music3",
        transport=httpx.MockTransport(handler),
    )
    result = await client.generate(_REQ)
    assert result.audio == b"RIFF....WAVEfmt "
    assert result.model == "MiniMaxAI/MiniMax-Music3"
    assert result.generation_seconds >= 0.0
    assert result.engine


async def test_non_2xx_raises_engine_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = SglangClient(base_url="http://sglang:8000", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(EngineError):
        await client.generate(_REQ)


async def test_unexpected_exception_is_normalized_to_engine_error() -> None:
    # A non-HTTP failure (transport handler raising) must surface as EngineError, so the runner's single
    # `except EngineError` frees the slot for every failure (adversarial case 3 / the seam contract).
    def handler(request: httpx.Request) -> httpx.Response:
        raise ValueError("unexpected transport-layer boom")

    client = SglangClient(base_url="http://sglang:8000", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(EngineError):
        await client.generate(_REQ)
