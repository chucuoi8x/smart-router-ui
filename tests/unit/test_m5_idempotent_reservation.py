"""RED tests Step 104 — idempotent reservation theo request/attempt identity."""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
from router import SmartRouter


@pytest.mark.asyncio
async def test_retry_same_request_reuses_reservation_id(monkeypatch):
    """Retry cùng request_id giữ nguyên reservation cho candidate đầu tiên."""
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("account:primary", "account", "requests", 3, 60))
    config = {
        "routes": {"chat": {"strategy": "priority", "candidates": [
            {"upstream": "primary", "model": "fast", "quota_resource_id": "account:primary"},
            {"upstream": "backup", "model": "slow", "quota_resource_id": "account:primary"},
        ]}},
        "upstreams": {
            "primary": {"base_url": "https://primary", "auth": {"token_env": "P_TOKEN"}},
            "backup": {"base_url": "https://backup", "auth": {"token_env": "P_TOKEN"}},
        },
        "logging": {"level": "CRITICAL"},
    }
    monkeypatch.setenv("P_TOKEN", "test-token")
    router = SmartRouter(config, quota_reservations=reservations)

    # Primary fail -> backup succeed
    primary = AsyncMock()
    backup = AsyncMock()

    # First call (primary attempt) -> TRANSIENT_NETWORK
    response_503 = AsyncMock(status_code=503, content=b'{"error":"overloaded"}', headers={})
    primary.post.return_value = response_503
    backup.post.return_value = AsyncMock(status_code=200, content=b'{"ok":true}', headers={})

    router.clients = {"primary": primary, "backup": backup}
    captured_ids = []
    original_reserve_many = reservations.reserve_many

    def capture_reserve_many(*, reservation_id, requests):
        captured_ids.append(reservation_id)
        return original_reserve_many(reservation_id=reservation_id, requests=requests)

    monkeypatch.setattr(reservations, "reserve_many", capture_reserve_many)
    result = await router.handle_messages(
        {"model": "chat", "messages": [{"role": "user", "content": "hello"}]},
        {"x-request-id": "req-104"}, "/v1/messages",
    )
    assert result.status_code == 200
    assert captured_ids == ["req-104"]


@pytest.mark.asyncio
async def test_new_request_gets_new_reservation(monkeypatch):
    """Request mới có x-request-id khác phải tạo reservation mới."""
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("account:primary", "account", "requests", 5, 60))
    config = {
        "routes": {"chat": {"strategy": "priority", "candidates": [
            {"upstream": "primary", "model": "fast", "quota_resource_id": "account:primary"},
        ]}},
        "upstreams": {"primary": {"base_url": "https://primary", "auth": {"token_env": "P_TOKEN"}}},
        "logging": {"level": "CRITICAL"},
    }
    monkeypatch.setenv("P_TOKEN", "test-token")
    router = SmartRouter(config, quota_reservations=reservations)
    primary = AsyncMock()
    primary.post.return_value = AsyncMock(status_code=200, content=b'{"ok":true}', headers={})
    router.clients = {"primary": primary}
    captured_ids = []
    original_reserve_many = reservations.reserve_many

    def capture_reserve_many(*, reservation_id, requests):
        captured_ids.append(reservation_id)
        return original_reserve_many(reservation_id=reservation_id, requests=requests)

    monkeypatch.setattr(reservations, "reserve_many", capture_reserve_many)
    for i in range(3):
        r = await router.handle_messages(
            {"model": "chat", "messages": [{"role": "user", "content": f"req-{i}"}]},
            {"x-request-id": f"req-{i}"}, "/v1/messages",
        )
        assert r.status_code == 200

    assert captured_ids == ["req-0", "req-1", "req-2"]
    assert reservations.snapshot("account:primary").used == 3
