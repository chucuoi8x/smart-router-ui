import pytest
import respx
from httpx import Response, AsyncClient, TimeoutException
import json
from typing import Any, Dict

from apps.gateway.providers.cliproxy_bridge import CLIProxyBridgeDriver


@pytest.fixture
def base_url() -> str:
    return "http://cliproxy:8080"


@pytest.fixture
def driver(base_url: str) -> CLIProxyBridgeDriver:
    return CLIProxyBridgeDriver(
        base_url=base_url,
        endpoint="/v1/chat/completions",
        timeout=5.0,
        headers={"X-Test": "test-value"},
    )


class TestNonStreaming:
    """Contract tests for execute() (non-streaming)."""

    @pytest.mark.asyncio
    async def test_successful_request_with_usage(self, driver: CLIProxyBridgeDriver, base_url: str):
        """Non-streaming request returns status, headers, body, usage."""
        response_body = {
            "id": "chatcmpl-123",
            "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
            "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        }
        with respx.mock:
            route = respx.post(f"{base_url}/v1/chat/completions")
            route.mock(return_value=Response(200, json=response_body))

            request = type("Request", (), {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": None,
                "json": {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            })()
            result = await driver.execute(None, request)

        assert result["status_code"] == 200
        assert result["headers"] is not None
        assert json.loads(result["body"]) == response_body
        assert result["usage"] == {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}
        assert "classification" in result
        assert result["classification"]["kind"] == "SUCCESS"

    @pytest.mark.asyncio
    async def test_handles_empty_usage(self, driver: CLIProxyBridgeDriver, base_url: str):
        """When usage is absent, parse_usage returns empty dict."""
        response_body = {
            "id": "chatcmpl-123",
            "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
        }
        with respx.mock:
            route = respx.post(f"{base_url}/v1/chat/completions")
            route.mock(return_value=Response(200, json=response_body))

            request = type("Request", (), {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": None,
                "json": {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            })()
            result = await driver.execute(None, request)

        assert result["status_code"] == 200
        assert result["usage"] == {}

    @pytest.mark.asyncio
    async def test_handles_non_json_response(self, driver: CLIProxyBridgeDriver, base_url: str):
        """Non-JSON body is returned as-is; usage remains empty."""
        with respx.mock:
            route = respx.post(f"{base_url}/v1/chat/completions")
            route.mock(return_value=Response(200, text="plain text response"))

            request = type("Request", (), {
                "method": "POST",
                "headers": {"Content-Type": "text/plain"},
                "body": b"hello",
                "json": None,
            })()
            result = await driver.execute(None, request)

        assert result["status_code"] == 200
        assert result["body"] == "plain text response"
        assert result["usage"] == {}

    @pytest.mark.asyncio
    async def test_rate_limit_error_classification(self, driver: CLIProxyBridgeDriver, base_url: str):
        """429 response is classified as rate limit."""
        with respx.mock:
            route = respx.post(f"{base_url}/v1/chat/completions")
            route.mock(return_value=Response(429, json={"error": "Rate limit exceeded"}))

            request = type("Request", (), {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": None,
                "json": {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            })()
            result = await driver.execute(None, request)

        assert result["status_code"] == 429
        assert result["classification"]["kind"] == "RATE_LIMIT"
        assert result["classification"]["retryable"] is True
        assert "rate" in result["classification"]["safe_message"].lower()

    @pytest.mark.asyncio
    async def test_quota_exhaustion_error(self, driver: CLIProxyBridgeDriver, base_url: str):
        """403/429 with quota message classified as quota_exceeded."""
        with respx.mock:
            route = respx.post(f"{base_url}/v1/chat/completions")
            route.mock(return_value=Response(403, json={"error": "Quota exceeded"}))

            request = type("Request", (), {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": None,
                "json": {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            })()
            result = await driver.execute(None, request)

        assert result["status_code"] == 403
        assert result["classification"]["kind"] == "QUOTA_EXHAUSTED"
        assert result["classification"]["retryable"] is False

    @pytest.mark.asyncio
    async def test_auth_error(self, driver: CLIProxyBridgeDriver, base_url: str):
        """401/403 classified as auth_error."""
        with respx.mock:
            route = respx.post(f"{base_url}/v1/chat/completions")
            route.mock(return_value=Response(401, json={"error": "Invalid API key"}))

            request = type("Request", (), {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": None,
                "json": {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            })()
            result = await driver.execute(None, request)

        assert result["status_code"] == 401
        assert result["classification"]["kind"] == "AUTH_EXPIRED"

    @pytest.mark.asyncio
    async def test_timeout_error(self, driver: CLIProxyBridgeDriver, base_url: str):
        """TimeoutException returns 408 error response."""
        with respx.mock:
            route = respx.post(f"{base_url}/v1/chat/completions")
            route.mock(side_effect=TimeoutException("Timeout"))

            request = type("Request", (), {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": None,
                "json": {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            })()
            result = await driver.execute(None, request)

        assert result["status_code"] == 408
        assert "timeout" in result["body"].lower()
        assert result["classification"]["kind"] == "TRANSIENT_NETWORK"

    @pytest.mark.asyncio
    async def test_other_http_error(self, driver: CLIProxyBridgeDriver, base_url: str):
        """500 error classified as transient/overload."""
        with respx.mock:
            route = respx.post(f"{base_url}/v1/chat/completions")
            route.mock(return_value=Response(500, text="Internal Server Error"))

            request = type("Request", (), {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": None,
                "json": {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            })()
            result = await driver.execute(None, request)

        assert result["status_code"] == 500
        assert result["classification"]["kind"] == "TRANSIENT_NETWORK"


class TestStreaming:
    """Contract tests for execute_stream()."""

    @pytest.mark.asyncio
    async def test_successful_stream_with_usage(self, driver: CLIProxyBridgeDriver, base_url: str):
        """Stream returns chunks and extracts usage after completion."""
        chunks = [
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        # Usage is sent in a final non-streaming chunk? In many providers, usage is in the last chunk or in a separate event.
        # We'll simulate that the last chunk includes usage.
        last_chunk_with_usage = b'data: {"choices":[{"delta":{}}],"usage":{"input_tokens":10,"output_tokens":20,"total_tokens":30}}\n\n'
        chunks_with_usage = chunks[:-1] + [last_chunk_with_usage]

        with respx.mock:
            route = respx.post(f"{base_url}/v1/chat/completions")
            route.mock(return_value=Response(200, content=b''.join(chunks_with_usage)))

            request = type("Request", (), {
                "method": "POST",
                "headers": {"Accept": "text/event-stream"},
                "body": None,
                "json": {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            })()
            collected = []
            async for chunk in driver.execute_stream(None, request):
                collected.append(chunk)

        # The driver yields whatever chunks the network provides; we shouldn't assert chunk count.
        # Instead, verify the concatenated data matches the expected content.
        expected_bytes = b''.join(chunks_with_usage)
        assert b''.join(collected) == expected_bytes
        # The driver should have stored usage internally
        assert driver._last_stream_usage == {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}

    @pytest.mark.asyncio
    async def test_stream_handles_malformed_json(self, driver: CLIProxyBridgeDriver, base_url: str):
        """Malformed chunks are forwarded as bytes; usage extraction falls back to empty."""
        malformed_chunks = [
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
            b'not valid json\n\n',
        ]
        with respx.mock:
            route = respx.post(f"{base_url}/v1/chat/completions")
            route.mock(return_value=Response(200, content=b''.join(malformed_chunks)))

            request = type("Request", (), {
                "method": "POST",
                "headers": {"Accept": "text/event-stream"},
                "body": None,
                "json": {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            })()
            collected = []
            async for chunk in driver.execute_stream(None, request):
                collected.append(chunk)

        # Don't assert chunk count; just verify the concatenated data matches expected.
        expected_bytes = b''.join(malformed_chunks)
        assert b''.join(collected) == expected_bytes
        # Usage parsing should ignore malformed and set empty
        assert driver._last_stream_usage == {}

    @pytest.mark.asyncio
    async def test_stream_error_status(self, driver: CLIProxyBridgeDriver, base_url: str):
        """Non-200 response yields empty chunk and no further data."""
        with respx.mock:
            route = respx.post(f"{base_url}/v1/chat/completions")
            route.mock(return_value=Response(429, json={"error": "Rate limited"}))

            request = type("Request", (), {
                "method": "POST",
                "headers": {"Accept": "text/event-stream"},
                "body": None,
                "json": {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            })()
            chunks = []
            async for chunk in driver.execute_stream(None, request):
                chunks.append(chunk)

        # We yielded one empty chunk to signal error.
        assert len(chunks) == 1
        assert chunks[0] == b""
        # Classification was computed but not returned in stream; _last_stream_usage remains empty.
        assert driver._last_stream_usage == {}

    @pytest.mark.asyncio
    async def test_stream_timeout(self, driver: CLIProxyBridgeDriver, base_url: str):
        """Timeout during stream yields empty chunk."""
        with respx.mock:
            route = respx.post(f"{base_url}/v1/chat/completions")
            route.mock(side_effect=TimeoutException("Stream timeout"))

            request = type("Request", (), {
                "method": "POST",
                "headers": {"Accept": "text/event-stream"},
                "body": None,
                "json": {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            })()
            chunks = []
            async for chunk in driver.execute_stream(None, request):
                chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0] == b""
        assert driver._last_stream_usage == {}


class TestParsingHelpers:
    def test_parse_usage_from_response_dict(self, driver: CLIProxyBridgeDriver):
        response = {
            "body": '{"usage": {"input_tokens": 5, "output_tokens": 15}}',
        }
        usage = driver.parse_usage(response)
        assert usage == {"input_tokens": 5, "output_tokens": 15}

    def test_parse_usage_from_response_with_usage_field(self, driver: CLIProxyBridgeDriver):
        response = {
            "usage": {"input_tokens": 7, "output_tokens": 11, "cost": 0.002},
        }
        usage = driver.parse_usage(response)
        assert usage == {"input_tokens": 7, "output_tokens": 11, "cost": 0.002}

    def test_parse_usage_from_response_object_with_text(self, driver: CLIProxyBridgeDriver):
        class Resp:
            text = '{"usage": {"input_tokens": 3, "output_tokens": 4}}'
        usage = driver.parse_usage(Resp())
        assert usage == {"input_tokens": 3, "output_tokens": 4}

    def test_parse_usage_fallback_to_stored(self, driver: CLIProxyBridgeDriver):
        driver._last_stream_usage = {"input_tokens": 100}
        usage = driver.parse_usage(None)
        assert usage == {"input_tokens": 100}


class TestCapabilitiesAndStubs:
    def test_capabilities(self, driver: CLIProxyBridgeDriver):
        caps = driver.capabilities()
        assert caps["supports_streaming"] is True

    @pytest.mark.asyncio
    async def test_validate_connection_ok(self, driver: CLIProxyBridgeDriver, base_url: str):
        with respx.mock:
            route = respx.head(base_url)
            route.mock(return_value=Response(200))
            result = await driver.validate_connection(None)
        assert result["status"] == "ok"
        assert result["status_code"] == 200

    @pytest.mark.asyncio
    async def test_validate_connection_error(self, driver: CLIProxyBridgeDriver, base_url: str):
        with respx.mock:
            route = respx.head(base_url)
            route.mock(side_effect=TimeoutException("Connection refused"))
            result = await driver.validate_connection(None)
        assert result["status"] == "error"
        assert "Connection refused" in result["message"]

    @pytest.mark.asyncio
    async def test_discover_models_success(self, driver: CLIProxyBridgeDriver, base_url: str):
        """Discover models returns list of models from CLIProxy /models endpoint."""
        models_response = {
            "models": [
                {"id": "gpt-4", "name": "GPT-4", "context_window": 8192},
                {"id": "claude-3", "name": "Claude 3", "context_window": 200000},
            ]
        }
        with respx.mock:
            route = respx.get(f"{base_url}/models")
            route.mock(return_value=Response(200, json=models_response))

            result = await driver.discover_models(None)

        assert len(result) == 2
        assert result[0]["id"] == "gpt-4"
        assert result[0]["name"] == "GPT-4"
        assert result[0]["context_window"] == 8192
        assert result[1]["id"] == "claude-3"

    @pytest.mark.asyncio
    async def test_discover_models_handles_error(self, driver: CLIProxyBridgeDriver, base_url: str):
        """When /models returns error, discover_models returns empty list."""
        with respx.mock:
            route = respx.get(f"{base_url}/models")
            route.mock(return_value=Response(500, text="Internal Server Error"))

            result = await driver.discover_models(None)

        assert result == []

    @pytest.mark.asyncio
    async def test_discover_models_handles_timeout(self, driver: CLIProxyBridgeDriver, base_url: str):
        """When /models times out, discover_models returns empty list."""
        with respx.mock:
            route = respx.get(f"{base_url}/models")
            route.mock(side_effect=TimeoutException("Timeout"))

            result = await driver.discover_models(None)

        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_quota_success(self, driver: CLIProxyBridgeDriver, base_url: str):
        """Fetch quota returns list of quota observations from CLIProxy /quota endpoint."""
        quota_response = {
            "resources": [
                {"metric": "tokens", "capacity": 1000000, "used": 250000, "window": "daily"},
                {"metric": "requests", "capacity": 1000, "used": 150, "window": "minute"},
            ]
        }
        with respx.mock:
            route = respx.get(f"{base_url}/quota")
            route.mock(return_value=Response(200, json=quota_response))

            result = await driver.fetch_quota(None)

        assert len(result) == 2
        assert result[0]["metric"] == "tokens"
        assert result[0]["capacity"] == 1000000
        assert result[0]["used"] == 250000
        assert result[0]["window"] == "daily"
        assert result[1]["metric"] == "requests"

    @pytest.mark.asyncio
    async def test_fetch_quota_handles_error(self, driver: CLIProxyBridgeDriver, base_url: str):
        """When /quota returns error, fetch_quota returns empty list."""
        with respx.mock:
            route = respx.get(f"{base_url}/quota")
            route.mock(return_value=Response(404, text="Not Found"))

            result = await driver.fetch_quota(None)

        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_quota_handles_timeout(self, driver: CLIProxyBridgeDriver, base_url: str):
        """When /quota times out, fetch_quota returns empty list."""
        with respx.mock:
            route = respx.get(f"{base_url}/quota")
            route.mock(side_effect=TimeoutException("Timeout"))

            result = await driver.fetch_quota(None)

        assert result == []


class TestHeaderFiltering:
    def test_filter_headers_removes_forbidden(self, driver: CLIProxyBridgeDriver):
        headers = {
            "Host": "example.com",
            "Content-Length": "100",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            "Expect": "100-continue",
            "Keep-Alive": "timeout=5",
            "X-Custom": "value",
        }
        filtered = driver._filter_headers(headers)
        assert "host" not in filtered
        assert "Content-Length" not in filtered
        assert "Connection" not in filtered
        assert "Transfer-Encoding" not in filtered
        assert "Expect" not in filtered
        assert "Keep-Alive" not in filtered
        # The driver preserves original header case when filtering
        assert "X-Custom" in filtered
        assert filtered["X-Custom"] == "value"