"""RED tests Step 101 — reservation theo candidate resource IDs."""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
from router import SmartRouter


@pytest.mark.asyncio
async def test_live_reservation_uses_selected_candidate_resource(monkeypatch):
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("account:primary", "account", "requests", 0, 60))
    reservations.add_resource(QuotaResource("account:backup", "account", "requests", 2, 60))
    config = {
        "routes": {"chat": {"strategy": "priority", "candidates": [
            {"upstream": "primary", "model": "fast", "quota_resource_id": "account:primary"},
            {"upstream": "backup", "model": "fast", "quota_resource_id": "account:backup"},
        ]}},
        "upstreams": {
            "primary": {"base_url": "https://primary", "auth": {"token_env": "P_TOKEN"}},
            "backup": {"base_url": "https://backup", "auth": {"token_env": "B_TOKEN"}},
        },
        "logging": {"level": "CRITICAL"},
    }
    router = SmartRouter(config, quota_reservations=reservations)
    monkeypatch.setenv("P_TOKEN", "test")
    monkeypatch.setenv("B_TOKEN", "test")
    response = AsyncMock(status_code=200, content=b'{"ok":true}', headers={})
    backup = AsyncMock()
    backup.post.return_value = response
    primary = AsyncMock()
    primary.post.return_value = response
    monkeypatch.setattr(router, "clients", {"primary": primary, "backup": backup})

    result = await router.handle_messages(
        {"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
        {}, "/v1/messages",
    )
    assert result.status_code == 200
    assert primary.post.call_count == 0
    assert backup.post.call_count == 1
    assert reservations.snapshot("account:primary").used == 0
    assert reservations.snapshot("account:backup").used == 1


@pytest.mark.asyncio
async def test_candidate_reservation_rejection_does_not_call_upstream(monkeypatch):
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("account:primary", "account", "requests", 0, 60))
    config = {
        "routes": {"chat": {"strategy": "priority", "candidates": [
            {"upstream": "primary", "model": "fast", "quota_resource_id": "account:primary"},
        ]}},
        "upstreams": {"primary": {"base_url": "https://primary", "auth": {"token_env": "P_TOKEN"}}},
        "logging": {"level": "CRITICAL"},
    }
    router = SmartRouter(config, quota_reservations=reservations)
    primary = AsyncMock()
    monkeypatch.setattr(router, "clients", {"primary": primary})
    result = await router.handle_messages(
        {"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
        {}, "/v1/messages",
    )
    assert result.status_code == 503
    assert json.loads(result.body)["error"]["type"] == "quota_exhausted"
    primary.post.assert_not_called()
