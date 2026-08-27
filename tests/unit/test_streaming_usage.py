import json
import os
from unittest.mock import AsyncMock, Mock, patch

import pytest

from router import SmartRouter
from apps.gateway.usage.ledger import InMemoryUsageLedger, UsageEvent


@pytest.fixture(autouse=True)
def _set_token_env(monkeypatch):
    """Every test gets the expected env-var value so _upstream_headers never rejects."""
    monkeypatch.setenv("SMART_ROUTER_UPSTREAM_TOKEN", "test-token")
    yield


def _make_upstream_config(base_url: str, driver_id: str | None = None) -> dict:
    cfg = {
        "base_url": base_url,
        "auth": {"mode": "bearer", "token_env": "SMART_ROUTER_UPSTREAM_TOKEN"},
    }
    if driver_id:
        cfg["driver_id"] = driver_id
    return cfg


def _make_router(route_name: str, candidates: list[dict], upstreams: dict[str, dict]) -> tuple[SmartRouter, InMemoryUsageLedger]:
    config = {
        "routes": {route_name: {"strategy": "priority", "candidates": candidates}},
        "upstreams": upstreams,
        "logging": {"level": "CRITICAL"},
    }
    ledger = InMemoryUsageLedger()
    return SmartRouter(config, usage_ledger=ledger), ledger


@pytest.mark.asyncio
async def test_streaming_anthropic_creates_consolidated_usage_event():
    """Test that streaming Anthropic SSE chunks parse and record a consolidated UsageEvent."""
    router, ledger = _make_router(
        "sonnet",
        [{"upstream": "anthropic-svc", "model": "claude-3-5-sonnet"}],
        {"anthropic-svc": _make_upstream_config("https://api.anthropic.com", "generic-anthropic")},
    )

    sse_chunks = [
        b'event: message_start\ndata: {"type": "message_start", "message": {"usage": {"input_tokens": 120, "output_tokens": 0}}}\n\n',
        b'event: content_block_delta\ndata: {"type": "content_block_delta", "delta": {"text": "Hello world"}}\n\n',
        b'event: message_delta\ndata: {"type": "message_delta", "usage": {"output_tokens": 80}}\n\n',
        b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]

    async def mock_iter():
        for chunk in sse_chunks:
            yield chunk

    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "text/event-stream"}
    mock_resp.aiter_bytes = lambda: mock_iter()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_resp

    mock_client = Mock()
    mock_client.stream.return_value = mock_ctx
    router.clients["anthropic-svc"] = mock_client

    # Execute stream_messages route
    body = {"model": "sonnet", "stream": True, "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    assert response.status_code == 200

    # Consume the streaming response body iterator to trigger end-of-stream processing
    chunks = [chunk async for chunk in response.body_iterator]
    assert len(chunks) > 0

    # Verify UsageEvent recorded in ledger with consolidated input and output tokens
    assert len(ledger._events) == 1
    evt = ledger._events[0]
    assert isinstance(evt, UsageEvent)
    assert evt.provider_connection_id == "anthropic-svc"
    assert evt.model_resource_id == "claude-3-5-sonnet"
    assert evt.input_tokens == 120
    assert evt.output_tokens == 80
    assert evt.total_tokens == 200
    assert evt.source == "provider_api"
    assert evt.confidence == "exact"


@pytest.mark.asyncio
async def test_streaming_openai_creates_usage_event():
    """Test that streaming OpenAI SSE chunks parse and record a UsageEvent."""
    router, ledger = _make_router(
        "gpt4",
        [{"upstream": "openai-svc", "model": "gpt-4o"}],
        {"openai-svc": _make_upstream_config("https://api.openai.com/v1", "generic-openai")},
    )

    sse_chunks = [
        b'data: {"id": "chatcmpl-1", "choices": [{"delta": {"content": "Hi"}}]}\n\n',
        b'data: {"id": "chatcmpl-1", "choices": [], "usage": {"prompt_tokens": 45, "completion_tokens": 55, "total_tokens": 100}}\n\n',
        b'data: [DONE]\n\n',
    ]

    async def mock_iter():
        for chunk in sse_chunks:
            yield chunk

    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "text/event-stream"}
    mock_resp.aiter_bytes = lambda: mock_iter()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_resp

    mock_client = Mock()
    mock_client.stream.return_value = mock_ctx
    router.clients["openai-svc"] = mock_client

    body = {"model": "gpt4", "stream": True, "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    assert response.status_code == 200

    chunks = [chunk async for chunk in response.body_iterator]
    assert len(chunks) > 0

    assert len(ledger._events) == 1
    evt = ledger._events[0]
    assert evt.provider_connection_id == "openai-svc"
    assert evt.model_resource_id == "gpt-4o"
    assert evt.input_tokens == 45
    assert evt.output_tokens == 55
    assert evt.total_tokens == 100


@pytest.mark.asyncio
async def test_streaming_zero_token_fabrication_mitigation():
    """Test that streaming responses with 0 tokens recorded do not pollute the ledger."""
    router, ledger = _make_router(
        "sonnet",
        [{"upstream": "anthropic-svc", "model": "claude-3-5-sonnet"}],
        {"anthropic-svc": _make_upstream_config("https://api.anthropic.com", "generic-anthropic")},
    )

    sse_chunks = [
        b'event: content_block_delta\ndata: {"type": "content_block_delta", "delta": {"text": "Hello"}}\n\n',
        b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]

    async def mock_iter():
        for chunk in sse_chunks:
            yield chunk

    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "text/event-stream"}
    mock_resp.aiter_bytes = lambda: mock_iter()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_resp

    mock_client = Mock()
    mock_client.stream.return_value = mock_ctx
    router.clients["anthropic-svc"] = mock_client

    body = {"model": "sonnet", "stream": True, "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    assert response.status_code == 200
    chunks = [chunk async for chunk in response.body_iterator]
    assert len(chunks) > 0

    # No usage tokens in SSE stream → no event recorded
    assert len(ledger._events) == 0
