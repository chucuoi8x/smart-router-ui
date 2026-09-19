import os
from unittest.mock import AsyncMock

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


def _build_mock_response(status_code: int, json_body: dict, content_bytes: bytes | None = None):
    """Return a real httpx.Response.

    P0-03: generic drivers delegate the exchange and read .text/.status_code/.headers,
    so fakes must be wire-real for the driver path to parse usage correctly.
    """
    import httpx
    import json as _json

    if content_bytes is None:
        content_bytes = _json.dumps(json_body).encode()
    return httpx.Response(status_code, content=content_bytes, headers={})


def _setup_client(router: SmartRouter, name: str, response):
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    # P0-03 driver delegation path issues the exchange through client.request().
    client.request = AsyncMock(return_value=response)
    router.clients[name] = client


@pytest.mark.asyncio
async def test_non_streaming_anthropic_creates_usage_event():
    """Test that a successful non-streaming Anthropic response parses and records UsageEvent."""
    router, ledger = _make_router(
        "sonnet",
        [{"upstream": "anthropic-svc", "model": "claude-3-5-sonnet"}],
        {"anthropic-svc": _make_upstream_config("https://api.anthropic.com", "generic-anthropic")},
    )

    resp = _build_mock_response(200, {"usage": {"input_tokens": 150, "output_tokens": 250}})
    _setup_client(router, "anthropic-svc", resp)

    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    assert response.status_code == 200
    assert len(ledger._events) == 1
    evt = ledger._events[0]
    assert isinstance(evt, UsageEvent)
    assert evt.provider_connection_id == "anthropic-svc"
    assert evt.model_resource_id == "claude-3-5-sonnet"
    assert evt.input_tokens == 150
    assert evt.output_tokens == 250
    assert evt.total_tokens == 400
    assert evt.source == "provider_api"
    assert evt.confidence == "exact"


@pytest.mark.asyncio
async def test_non_streaming_openai_creates_usage_event():
    """Test that a successful non-streaming OpenAI response parses and records UsageEvent."""
    router, ledger = _make_router(
        "gpt4",
        [{"upstream": "openai-svc", "model": "gpt-4o"}],
        {"openai-svc": _make_upstream_config("https://api.openai.com/v1", "generic-openai")},
    )

    resp = _build_mock_response(200, {"usage": {"prompt_tokens": 80, "completion_tokens": 120, "total_tokens": 200}})
    _setup_client(router, "openai-svc", resp)

    body = {"model": "gpt4", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/chat/completions")

    assert response.status_code == 200
    assert len(ledger._events) == 1
    evt = ledger._events[0]
    assert evt.provider_connection_id == "openai-svc"
    assert evt.model_resource_id == "gpt-4o"
    assert evt.input_tokens == 80
    assert evt.output_tokens == 120
    assert evt.total_tokens == 200
    assert evt.source == "provider_api"
    assert evt.confidence == "exact"


@pytest.mark.asyncio
async def test_non_streaming_gemini_creates_usage_event():
    """Test that a successful non-streaming Gemini response parses and records UsageEvent."""
    router, ledger = _make_router(
        "gemini-pro",
        [{"upstream": "gemini-svc", "model": "gemini-1.5-pro"}],
        {"gemini-svc": _make_upstream_config("https://generativelanguage.googleapis.com", "generic-gemini")},
    )

    resp = _build_mock_response(200, {"usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 150}})
    _setup_client(router, "gemini-svc", resp)

    body = {"model": "gemini-pro", "contents": [{"parts": [{"text": "hello"}]}]}
    response = await router.handle_messages(body, {}, "/v1/models/gemini-1.5-pro:generateContent")

    assert response.status_code == 200
    assert len(ledger._events) == 1
    evt = ledger._events[0]
    assert evt.provider_connection_id == "gemini-svc"
    assert evt.model_resource_id == "gemini-1.5-pro"
    assert evt.input_tokens == 50
    assert evt.output_tokens == 150
    assert evt.total_tokens == 200
    assert evt.source == "provider_api"
    assert evt.confidence == "exact"


@pytest.mark.asyncio
async def test_non_streaming_non_json_response_graceful_fallback():
    """Test that if the response body is not valid JSON, we skip usage parse gracefully."""
    router, ledger = _make_router(
        "sonnet",
        [{"upstream": "anthropic-svc", "model": "claude-3-5-sonnet"}],
        {"anthropic-svc": _make_upstream_config("https://api.anthropic.com", "generic-anthropic")},
    )

    resp = _build_mock_response(200, {}, content_bytes=b'invalid-binary-non-json')
    _setup_client(router, "anthropic-svc", resp)

    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    # Route succeeds but zero UsageEvents should be recorded
    assert response.status_code == 200
    assert len(ledger._events) == 0


@pytest.mark.asyncio
async def test_non_streaming_unresolved_driver_graceful_fallback():
    """Test that if no driver can be resolved for the upstream, we skip usage parse gracefully."""
    router, ledger = _make_router(
        "custom",
        [{"upstream": "test-upstream", "model": "custom-model"}],
        {"test-upstream": _make_upstream_config("https://custom.invalid", None)},  # no driver_id
    )

    resp = _build_mock_response(200, {"usage": {"input_tokens": 10, "output_tokens": 20}})
    _setup_client(router, "test-upstream", resp)

    body = {"model": "custom", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    assert response.status_code == 200
    assert len(ledger._events) == 0  # skipped parse since driver can't be resolved


@pytest.mark.asyncio
async def test_non_streaming_ledger_failure_is_ignored():
    """Test that ledger exceptions do not break the data-plane traffic."""
    class FailingLedger:
        def record_request(self, *args, **kwargs):
            pass
        def record_attempt(self, *args, **kwargs):
            pass
        def record_usage(self, event):
            raise RuntimeError("Database connection down!")

    router, _ = _make_router(
        "sonnet",
        [{"upstream": "anthropic-svc", "model": "claude-3-5-sonnet"}],
        {"anthropic-svc": _make_upstream_config("https://api.anthropic.com", "generic-anthropic")},
    )
    router._usage_ledger = FailingLedger()

    resp = _build_mock_response(200, {"usage": {"input_tokens": 100, "output_tokens": 200}})
    _setup_client(router, "anthropic-svc", resp)

    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    # Routing must succeed completely despite database ledger crash
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_non_streaming_heuristic_driver_resolution():
    """Test that heuristic keyword matching on upstream ID resolves to driver properly."""
    router, ledger = _make_router(
        "sonnet",
        [{"upstream": "my-anthropic-pool", "model": "claude-3-5-sonnet"}],
        {"my-anthropic-pool": _make_upstream_config("https://api.anthropic.com", None)},
    )

    resp = _build_mock_response(200, {"usage": {"input_tokens": 30, "output_tokens": 70}})
    _setup_client(router, "my-anthropic-pool", resp)

    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    assert response.status_code == 200
    assert len(ledger._events) == 1
    assert ledger._events[0].provider_connection_id == "my-anthropic-pool"
    assert ledger._events[0].input_tokens == 30
    assert ledger._events[0].output_tokens == 70


@pytest.mark.asyncio
async def test_non_streaming_failover_records_exactly_one_usage_event():
    """Test that during a failover chain, only the successful attempt records a UsageEvent."""
    upstreams = {
        "primary": _make_upstream_config("https://api.primary.invalid", "generic-anthropic"),
        "secondary": _make_upstream_config("https://api.secondary.invalid", "generic-anthropic"),
    }
    router, ledger = _make_router(
        "sonnet",
        [
            {"upstream": "primary", "model": "claude-3-5-sonnet"},
            {"upstream": "secondary", "model": "claude-3-5-sonnet"},
        ],
        upstreams,
    )

    # Primary fails with 502
    primary_resp = _build_mock_response(502, {}, content_bytes=b'bad gateway')
    _setup_client(router, "primary", primary_resp)

    # Secondary succeeds
    secondary_resp = _build_mock_response(200, {"usage": {"input_tokens": 12, "output_tokens": 18}})
    _setup_client(router, "secondary", secondary_resp)

    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    assert response.status_code == 200
    assert len(ledger._events) == 1
    evt = ledger._events[0]
    assert evt.provider_connection_id == "secondary"
    assert evt.input_tokens == 12
    assert evt.output_tokens == 18


@pytest.mark.asyncio
async def test_non_streaming_usage_event_includes_catalog_cost_estimate():
    router, ledger = _make_router(
        "sonnet",
        [{"upstream": "anthropic-svc", "model": "claude-3-5-sonnet"}],
        {"anthropic-svc": _make_upstream_config("https://api.anthropic.com", "generic-anthropic")},
    )
    router.catalog = {
        "prices": {
            "claude-3-5-sonnet": {
                "input_per_million": 3.0,
                "output_per_million": 15.0,
            }
        }
    }

    resp = _build_mock_response(200, {"usage": {"input_tokens": 1000, "output_tokens": 2000}})
    _setup_client(router, "anthropic-svc", resp)

    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    assert response.status_code == 200
    assert len(ledger._events) == 1
    evt = ledger._events[0]
    assert evt.actual_cost == pytest.approx(0.033)
    assert evt.currency == "USD"


@pytest.mark.asyncio
async def test_non_streaming_usage_cost_stays_unknown_when_price_missing():
    router, ledger = _make_router(
        "sonnet",
        [{"upstream": "anthropic-svc", "model": "claude-3-5-sonnet"}],
        {"anthropic-svc": _make_upstream_config("https://api.anthropic.com", "generic-anthropic")},
    )
    router.catalog = {"prices": {}}

    resp = _build_mock_response(200, {"usage": {"input_tokens": 1000, "output_tokens": 2000}})
    _setup_client(router, "anthropic-svc", resp)

    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    assert response.status_code == 200
    assert len(ledger._events) == 1
    evt = ledger._events[0]
    assert evt.actual_cost is None
    assert evt.currency is None


@pytest.mark.asyncio
async def test_count_tokens_endpoint_skips_usage_event():
    """/v1/messages/count_tokens does not record a UsageEvent."""
    router, ledger = _make_router(
        "sonnet",
        [{"upstream": "anthropic-svc", "model": "claude-3-5-sonnet"}],
        {"anthropic-svc": _make_upstream_config("https://api.anthropic.com", "generic-anthropic")},
    )

    resp = _build_mock_response(200, {"input_tokens": 42})
    _setup_client(router, "anthropic-svc", resp)

    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages/count_tokens")

    # count_tokens returns successfully but should NOT create any UsageEvent
    assert response.status_code == 200
    assert len(ledger._events) == 0


@pytest.mark.asyncio
async def test_zero_token_fabrication_mitigation():
    """Test that parse_usage returning zero tokens does not create a fabricated UsageEvent."""
    router, ledger = _make_router(
        "sonnet",
        [{"upstream": "anthropic-svc", "model": "claude-3-5-sonnet"}],
        {"anthropic-svc": _make_upstream_config("https://api.anthropic.com", "generic-anthropic")},
    )

    resp = _build_mock_response(200, {"usage": {"input_tokens": 0, "output_tokens": 0}})
    _setup_client(router, "anthropic-svc", resp)

    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    # Route succeeds but zero-token fabrication is prevented — no UsageEvent recorded
    assert response.status_code == 200
    assert len(ledger._events) == 0
