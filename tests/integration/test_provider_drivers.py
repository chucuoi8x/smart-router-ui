"""P0-03: generic drivers must speak real HTTP, verified with httpx.MockTransport.

Every assertion checks the *wire*: method, absolute URL, auth headers, request
body and parsed response.  No driver method is allowed to return canned data.
"""
import json

import httpx
import pytest

from apps.gateway.providers.driver_context import DriverContext
from apps.gateway.providers.generic_anthropic import GenericAnthropicDriver
from apps.gateway.providers.generic_gemini import GenericGeminiDriver
from apps.gateway.providers.generic_openai import GenericOpenAIDriver


class Recorder:
    def __init__(self, responder):
        self.requests = []
        self._responder = responder

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "driver issued no HTTP request"
        return self.requests[-1]


def _client(recorder: Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))


def _ctx(base_url: str, credential: dict) -> DriverContext:
    return DriverContext(connection_id="c1", base_url=base_url, credential=credential)


async def _sseResponder(body: str):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


# ── OpenAI-compatible ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_openai_discovery_builds_real_bearer_request():
    rec = Recorder(lambda r: httpx.Response(200, json={"data": [{"id": "gpt-4o"}, {"id": "gpt-3.5-turbo"}]}))
    driver = GenericOpenAIDriver(client=_client(rec))
    models = await driver.discover_models(_ctx("https://api.openai.com/v1", {"api_key": "sk-123"}))

    assert [m["id"] for m in models] == ["gpt-4o", "gpt-3.5-turbo"]
    req = rec.last
    assert req.method == "GET"
    assert str(req.url) == "https://api.openai.com/v1/models"
    assert req.headers["authorization"] == "Bearer sk-123"


@pytest.mark.asyncio
async def test_openai_execute_serializes_body_and_parses_usage_and_rate_limits():
    payload = {
        "id": "cmpl-1",
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                  "prompt_tokens_details": {"cached_tokens": 4}},
    }

    def responder(request):
        return httpx.Response(200, json=payload, headers={
            "x-ratelimit-remaining-requests": "49",
            "x-ratelimit-remaining-tokens": "9000",
            "retry-after": "2",
        })

    rec = Recorder(responder)
    driver = GenericOpenAIDriver(client=_client(rec))
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions",
                            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}]})

    class _Req:
        endpoint = "chat/completions"
        body = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}]}).encode()
        headers = {}
        method = "POST"

    result = await driver.execute(_ctx("https://api.openai.com/v1", {"api_key": "sk-abc"}), _Req())

    req = rec.last
    assert str(req.url) == "https://api.openai.com/v1/chat/completions"
    assert json.loads(req.content) == {"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}]}
    assert req.headers["authorization"] == "Bearer sk-abc"

    assert result["status_code"] == 200
    assert result["usage"]["input_tokens"] == 11
    assert result["usage"]["output_tokens"] == 7
    assert result["usage"]["cached_tokens"] == 4
    assert result["usage"]["confidence"] == "exact"
    meta = driver.rate_limit_metadata(None, result["headers"])
    assert meta["remaining"]["requests"] == "49"
    assert meta["remaining"]["tokens"] == "9000"
    assert meta["retry_after"] == "2"


@pytest.mark.asyncio
async def test_openai_stream_passes_through_sse_and_collects_usage():
    sse = (
        'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"llo"}}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
        "data: [DONE]\n\n"
    )
    rec = Recorder(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse))
    driver = GenericOpenAIDriver(client=_client(rec))

    class _Req:
        endpoint = "chat/completions"
        body = b'{"model":"gpt-4o","stream":true}'
        headers = {}
        method = "POST"

    chunks = [c async for c in driver.execute_stream(_ctx("https://api.openai.com/v1", {"api_key": "k"}), _Req())]
    assert b"".join(chunks).decode() == sse
    assert rec.last.headers["authorization"] == "Bearer k"
    assert driver.last_stream_usage["input_tokens"] == 3
    assert driver.last_stream_usage["output_tokens"] == 2


@pytest.mark.asyncio
async def test_openai_429_classified_as_retryable_rate_limit():
    rec = Recorder(lambda r: httpx.Response(429, json={"error": {"message": "slow down"}},
                                           headers={"retry-after": "7"}))
    driver = GenericOpenAIDriver(client=_client(rec))

    class _Req:
        endpoint = "chat/completions"
        body = b"{}"
        headers = {}
        method = "POST"

    result = await driver.execute(_ctx("https://api.openai.com/v1", {"api_key": "k"}), _Req())
    assert result["status_code"] == 429
    assert result["classification"]["retryable"] is True
    assert result["classification"]["kind"] in {"RATE_LIMIT", "UPSTREAM_RATE_LIMIT", "THROTTLED"}


@pytest.mark.asyncio
async def test_openai_timeout_surfaces_as_error_not_silence():
    def responder(request):
        raise httpx.TimeoutException("boom")

    rec = Recorder(responder)
    driver = GenericOpenAIDriver(client=_client(rec))

    class _Req:
        endpoint = "chat/completions"
        body = b"{}"
        headers = {}
        method = "POST"

    result = await driver.execute(_ctx("https://api.openai.com/v1", {"api_key": "k"}), _Req())
    assert result["status_code"] in {408, 0}
    assert result["classification"]["retryable"] is True
    assert rec.requests, "timeout must come from an attempted request"


@pytest.mark.asyncio
async def test_validate_connection_reports_bad_credential_as_error():
    rec = Recorder(lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    driver = GenericOpenAIDriver(client=_client(rec))
    outcome = await driver.validate_connection(_ctx("https://api.openai.com/v1", {"api_key": "nope"}))
    assert outcome["status"] == "error"
    assert rec.last.headers["authorization"] == "Bearer nope"


@pytest.mark.asyncio
async def test_custom_base_url_and_headers_are_honored():
    rec = Recorder(lambda r: httpx.Response(200, json={"data": []}))
    driver = GenericOpenAIDriver(
        base_url="https://gateway.internal.example/oa",
        headers={"x-tenant": "acme"},
        client=_client(rec),
    )
    await driver.discover_models(_ctx("ignored-because-instance-base-url-wins", {"api_key": "k"}))
    assert str(rec.last.url) == "https://gateway.internal.example/oa/models"
    assert rec.last.headers["x-tenant"] == "acme"


# ── Anthropic-compatible ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_anthropic_execute_uses_x_api_key_and_version_headers():
    payload = {"id": "msg_1", "usage": {"input_tokens": 5, "output_tokens": 9,
                                        "cache_read_input_tokens": 2}}
    rec = Recorder(lambda r: httpx.Response(200, json=payload))
    driver = GenericAnthropicDriver(client=_client(rec))

    class _Req:
        endpoint = "v1/messages"
        body = json.dumps({"model": "claude-sonnet", "max_tokens": 16, "messages": []}).encode()
        headers = {}
        method = "POST"

    result = await driver.execute(_ctx("https://api.anthropic.com", {"api_key": "sk-ant-1"}), _Req())
    req = rec.last
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "sk-ant-1"
    assert req.headers["anthropic-version"]
    assert result["usage"]["input_tokens"] == 5
    assert result["usage"]["cache_read_input_tokens"] == 2


@pytest.mark.asyncio
async def test_anthropic_stream_usage_spans_message_start_and_delta():
    sse = (
        'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":12,"output_tokens":1}}}\n\n'
        'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":34}}\n\n'
        "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
    )
    rec = Recorder(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse))
    driver = GenericAnthropicDriver(client=_client(rec))

    class _Req:
        endpoint = "v1/messages"
        body = b'{"stream":true}'
        headers = {}
        method = "POST"

    out = b"".join([c async for c in driver.execute_stream(_ctx("https://api.anthropic.com", {"api_key": "k"}), _Req())])
    assert out.decode() == sse
    usage = driver.last_stream_usage
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 34


# ── Gemini-compatible ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_gemini_discovery_uses_x_goog_api_key():
    body = {"models": [{"name": "models/gemini-1.5-pro", "supportedGenerationMethods": ["generateContent"]}]}
    rec = Recorder(lambda r: httpx.Response(200, json=body))
    driver = GenericGeminiDriver(client=_client(rec))
    models = await driver.discover_models(_ctx("https://generativelanguage.googleapis.com", {"api_key": "gk"}))
    assert models[0]["id"] == "gemini-1.5-pro"
    assert rec.last.headers["x-goog-api-key"] == "gk"
    assert str(rec.last.url).startswith("https://generativelanguage.googleapis.com/v1beta/models")


@pytest.mark.asyncio
async def test_gemini_execute_builds_model_path_and_parses_usage_metadata():
    payload = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}],
               "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 4, "totalTokenCount": 10}}
    rec = Recorder(lambda r: httpx.Response(200, json=payload))
    driver = GenericGeminiDriver(client=_client(rec))

    class _Req:
        endpoint = "generateContent"
        body = json.dumps({"model_id": "gemini-1.5-flash", "contents": []}).encode()
        headers = {}
        method = "POST"

    result = await driver.execute(_ctx("https://generativelanguage.googleapis.com", {"api_key": "gk"}), _Req())
    req = rec.last
    assert "/models/gemini-1.5-flash:generateContent" in str(req.url)
    assert req.headers["x-goog-api-key"] == "gk"
    assert result["usage"] == {"input_tokens": 6, "output_tokens": 4, "total_tokens": 10,
                               "source": "provider_api", "confidence": "exact"}
