"""RED tests Step 102 — TPM reservation dùng estimated input tokens."""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
from router import SmartRouter


@pytest.mark.asyncio
async def test_tpm_reservation_uses_estimated_input_tokens(monkeypatch):
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("tpm:primary", "account", "tokens", 5, 60))
    config = {
        "routes": {"chat": {"strategy": "priority", "candidates": [
            {"upstream": "primary", "model": "fast", "quota_resource_id": "tpm:primary"},
        ]}},
        "upstreams": {"primary": {"base_url": "https://primary", "auth": {"token_env": "P_TOKEN"}}},
        "logging": {"level": "CRITICAL"},
    }
    router = SmartRouter(config, quota_reservations=reservations)
    primary = AsyncMock()
    monkeypatch.setattr(router, "clients", {"primary": primary})
    result = await router.handle_messages(
        {"model": "chat", "messages": [{"role": "user", "content": "this input is longer than five tokens"}]},
        {}, "/v1/messages",
    )
    assert result.status_code == 503
    assert json.loads(result.body)["error"]["type"] == "quota_exhausted"
    primary.post.assert_not_called()
