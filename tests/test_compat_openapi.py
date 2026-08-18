"""OpenAPI contract regression for the cloud route (AR-1).

Asserts the generated document actually describes the public request / success / error shapes, so a cloud
client or codegen can discover them — not just that the document is drift-free.
"""

from app.main import config_from_env, create_app


def _post_op() -> dict:
    spec = create_app(config=config_from_env({})).openapi()
    return spec["paths"]["/v1/music_generation"]["post"], spec


def test_post_documents_request_body() -> None:
    post, _spec = _post_op()
    schema = post["requestBody"]["content"]["application/json"]["schema"]
    props = schema["properties"]
    assert set(schema["required"]) == {"model", "prompt", "lyrics"}
    for field in ("model", "prompt", "lyrics", "stream", "output_format", "audio_setting", "seed", "max_new_tokens"):
        assert field in props, f"request schema missing {field}"
    assert props["output_format"]["enum"] == ["hex", "url"]
    assert props["audio_setting"]["properties"]["bitrate"]["enum"] == [32000, 64000, 128000, 256000]


def test_post_documents_success_and_error_responses() -> None:
    post, spec = _post_op()
    assert set(post["responses"]) >= {"200", "400", "503"}
    schemas = spec["components"]["schemas"]
    assert {"MusicGenerationResponse", "ErrorEnvelope", "BaseResp", "ExtraInfo", "MusicData"} <= set(schemas)
    # the success envelope carries the documented cloud fields
    success = schemas["MusicGenerationResponse"]["properties"]
    for field in ("data", "trace_id", "extra_info", "analysis_info", "base_resp"):
        assert field in success


def test_post_description_states_read_timeout_and_reserved_code() -> None:
    post, _spec = _post_op()
    assert "read timeout of at least 1200 seconds" in post["description"]  # R-06
    assert "5000" in post["description"]  # reserved local failure code documented (base-resp-errors.md)


def test_result_route_documents_audio_and_error_responses() -> None:
    spec = create_app(config=config_from_env({})).openapi()
    get = spec["paths"]["/v1/music_generation/result/{trace_id}"]["get"]
    assert "audio/mpeg" in get["responses"]["200"]["content"]
    assert set(get["responses"]) >= {"200", "404", "503"}
