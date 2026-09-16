import types
import pytest

from router import SmartRouter
from apps.gateway.usage.ledger import InMemoryUsageLedger


def _ledger_router(route: str, candidate: dict, upstream: dict):
    upstream_cfg = {
        "base_url": upstream["base_url"],
        "auth": {"mode": "bearer", "token_env": "SMART_ROUTER_UPSTREAM_TOKEN"},
        "driver_id": upstream.get("driver_id"),
    }
    cfg = {
        "routes": {route: {"strategy": "priority", "candidates": [candidate]}},
        "upstreams": {candidate["upstream"]: upstream_cfg},
        "logging": {"level": "CRITICAL"},
    }
    ledger = InMemoryUsageLedger()
    return SmartRouter(cfg, usage_ledger=ledger), ledger


@pytest.mark.asyncio
async def test_delegating_driver_routes_and_records_usage(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_UPSTREAM_TOKEN", "test-token")
    router, ledger = _ledger_router(
        "cliproxy-route",
        {"upstream": "cliproxy", "model": "gpt-4"},
        {"base_url": "http://cliproxy.test", "driver_id": "cliproxy-bridge"},
    )
    called = {}

    async def fake_execute(self, ctx, request):
        called["url"] = self.base_url
        called["endpoint"] = self.endpoint
        return {
            "status_code": 200,
            "headers": {},
            "body": '{"id":"ok","usage":{"input_tokens":10,"output_tokens":20,"total_tokens":30}}',
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 30,
                "source": "provider_api",
                "confidence": "exact",
            },
            "classification": {"kind": "SUCCESS", "retryable": False, "scope": "request"},
        }

    monkeypatch.setattr("apps.gateway.providers.cliproxy_bridge.CLIProxyBridgeDriver.execute", fake_execute)

    # Keep a real client dict entry so router's client lookup succeeds — driver branch still consumes it.
    from unittest.mock import AsyncMock

    router.clients["cliproxy"] = AsyncMock()

    resp = await router.handle_messages({"model": "cliproxy-route", "messages": []}, {}, "/v1/messages")
    assert resp.status_code == 200
    assert called["url"] == "http://cliproxy.test"
    assert called["endpoint"] == "v1/messages"
    assert len(ledger._events) == 1
    evt = ledger._events[0]
    assert evt.input_tokens == 10 and evt.output_tokens == 20
    assert evt.source == "provider_api" and evt.confidence == "exact"


@pytest.mark.asyncio
async def test_non_delegating_driver_does_not_use_driver_path(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_UPSTREAM_TOKEN", "test-token")
    router, ledger = _ledger_router(
        "sonnet",
        {"upstream": "anthropic-svc", "model": "claude-3-5-sonnet"},
        {"base_url": "https://api.anthropic.com", "driver_id": "generic-anthropic"},
    )
    from unittest.mock import AsyncMock, Mock

    resp = AsyncMock()
    resp.status_code = 200
    resp.content = b'{"id":"ok"}'
    resp.headers = {}
    resp.json = Mock(return_value={"usage": {"input_tokens": 1, "output_tokens": 2}})
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    router.clients["anthropic-svc"] = client

    r = await router.handle_messages({"model": "sonnet", "messages": []}, {}, "/v1/messages")
    assert r.status_code == 200
    client.post.assert_awaited_once()
    assert ledger._events[0].provider_connection_id == "anthropic-svc"
