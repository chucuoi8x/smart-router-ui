"""RED tests Step 107 — chuyển reservation khi failover candidate."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
from router import SmartRouter


@pytest.mark.asyncio
async def test_failover_transfers_reservation_to_backup_resource(monkeypatch):
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("account:primary", "account", "requests", 2, 60))
    reservations.add_resource(QuotaResource("account:backup", "account", "requests", 2, 60))
    config = {
        "routes": {"chat": {"strategy": "priority", "candidates": [
            {"upstream": "primary", "model": "fast", "quota_resource_id": "account:primary"},
            {"upstream": "backup", "model": "slow", "quota_resource_id": "account:backup"},
        ]}},
        "upstreams": {
            "primary": {"base_url": "https://primary", "auth": {"token_env": "P_TOKEN"}},
            "backup": {"base_url": "https://backup", "auth": {"token_env": "P_TOKEN"}},
        },
        "logging": {"level": "CRITICAL"},
    }
    monkeypatch.setenv("P_TOKEN", "test-token")
    router = SmartRouter(config, quota_reservations=reservations)
    primary = AsyncMock()
    backup = AsyncMock()
    # 503 quota triggers failover; backup succeeds
    primary.post.return_value = AsyncMock(status_code=503, content=b'{"error":"rate"}', headers={})
    backup.post.return_value = AsyncMock(status_code=200, content=b'{"ok":true}', headers={})
    monkeypatch.setattr(router, "clients", {"primary": primary, "backup": backup})

    result = await router.handle_messages(
        {"model": "chat", "messages": [{"role": "user", "content": "hello"}]},
        {"x-request-id": "req-transfer-1"},
        "/v1/messages",
    )
    assert result.status_code == 200
    # Primary reservation must not remain held; backup consumed one request.
    assert reservations.snapshot("account:primary").used == 0
    assert reservations.snapshot("account:backup").used == 1
