"""RED tests Step 105 — wiring safety_buffer_ratio vào risk_buffer."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
from router import SmartRouter


@pytest.mark.asyncio
async def test_token_reservation_includes_safety_buffer(monkeypatch):
    """Critical preset ratio 0.08 → reservation 10 tokens yêu cầu 11, vượt limit 10 → 503."""
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("tpm:primary", "account", "tokens", 10, 60))
    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "fast", "quota_resource_id": "tpm:primary"}],
            }
        },
        "upstreams": {"primary": {"base_url": "https://primary", "auth": {"token_env": "P_TOKEN"}}},
        "logging": {"level": "CRITICAL"},
        "smart_scheduler": {"enabled": True, "mode": "active", "preset": "critical"},
    }
    monkeypatch.setenv("P_TOKEN", "test-token")
    router = SmartRouter(config, quota_reservations=reservations)
    # deterministic estimate: 10 input tokens
    monkeypatch.setattr(router, "_estimate_input_tokens", lambda body: 10)
    primary = AsyncMock()
    monkeypatch.setattr(router, "clients", {"primary": primary})

    result = await router.handle_messages(
        {"model": "chat", "messages": [{"role": "user", "content": "hello"}]},
        {},
        "/v1/messages",
    )
    assert result.status_code == 503
    import json

    assert json.loads(result.body)["error"]["type"] == "quota_exhausted"
    primary.post.assert_not_called()


@pytest.mark.asyncio
async def test_requests_metric_not_affected_by_token_buffer(monkeypatch):
    """Requests metric không bị cộng buffer — limit 1 vẫn cho 1 request."""
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("account:primary", "account", "requests", 1, 60))
    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "fast", "quota_resource_id": "account:primary"}],
            }
        },
        "upstreams": {"primary": {"base_url": "https://primary", "auth": {"token_env": "P_TOKEN"}}},
        "logging": {"level": "CRITICAL"},
        "smart_scheduler": {"enabled": True, "mode": "active", "preset": "critical"},
    }
    monkeypatch.setenv("P_TOKEN", "test-token")
    router = SmartRouter(config, quota_reservations=reservations)
    primary = AsyncMock()
    primary.post.return_value = AsyncMock(status_code=200, content=b'{"ok":true}', headers={})
    monkeypatch.setattr(router, "clients", {"primary": primary})

    result = await router.handle_messages(
        {"model": "chat", "messages": [{"role": "user", "content": "hello"}]},
        {"x-request-id": "req-buf-1"},
        "/v1/messages",
    )
    assert result.status_code == 200
