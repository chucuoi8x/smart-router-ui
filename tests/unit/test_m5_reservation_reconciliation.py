"""RED tests Step 103 — output token reserve và multi-resource reconcile."""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
from router import SmartRouter


@pytest.mark.asyncio
async def test_token_reservation_includes_requested_output_tokens(monkeypatch):
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("tpm:primary", "account", "tokens", 30, 60))
    config = {
        "routes": {"chat": {"strategy": "priority", "candidates": [
            {"upstream": "primary", "model": "fast", "quota_resource_id": "tpm:primary"},
        ]}},
        "upstreams": {"primary": {"base_url": "https://primary", "auth": {"token_env": "P_TOKEN"}}},
        "logging": {"level": "CRITICAL"},
    }
    monkeypatch.setenv("P_TOKEN", "test-token")
    router = SmartRouter(config, quota_reservations=reservations)
    primary = AsyncMock()
    monkeypatch.setattr(router, "clients", {"primary": primary})
    result = await router.handle_messages(
        {"model": "chat", "max_tokens": 10, "messages": [{"role": "user", "content": "long input"}]},
        {}, "/v1/messages",
    )
    assert result.status_code == 503
    assert json.loads(result.body)["error"]["type"] == "quota_exhausted"
    primary.post.assert_not_called()


@pytest.mark.asyncio
async def test_multi_resource_reservation_reconciles_all_resources(monkeypatch):
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("account:primary", "account", "requests", 2, 60))
    reservations.add_resource(QuotaResource("tpm:primary", "account", "tokens", 100, 60))
    config = {
        "routes": {"chat": {"strategy": "priority", "candidates": [
            {"upstream": "primary", "model": "fast", "quota_resource_ids": ["account:primary", "tpm:primary"]},
        ]}},
        "upstreams": {"primary": {"base_url": "https://primary", "auth": {"token_env": "P_TOKEN"}}},
        "logging": {"level": "CRITICAL"},
    }
    monkeypatch.setenv("P_TOKEN", "test-token")
    router = SmartRouter(config, quota_reservations=reservations)
    response = AsyncMock(status_code=200, content=b'{"ok":true}', headers={})
    primary = AsyncMock()
    primary.post.return_value = response
    monkeypatch.setattr(router, "clients", {"primary": primary})
    result = await router.handle_messages(
        {"model": "chat", "messages": [{"role": "user", "content": "hello"}]},
        {}, "/v1/messages",
    )
    assert result.status_code == 200
    assert reservations.snapshot("account:primary").used == 1
    assert reservations.snapshot("tpm:primary").used == 1
