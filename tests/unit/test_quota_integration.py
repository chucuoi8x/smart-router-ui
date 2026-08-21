import asyncio
import json
import pytest
from unittest.mock import AsyncMock

from router import SmartRouter
from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource


@pytest.mark.asyncio
async def test_quota_exhaustion_rejects_request(monkeypatch):
    # Arrange: set up quota with limit 0 for the route
    reservations = InMemoryQuotaReservations()
    resource = QuotaResource(
        resource_id="model:claude-router-main",
        scope="tenant",
        metric="requests",
        limit=0,
        window_seconds=60,
        hard_limit=True,
    )
    reservations.add_resource(resource)

    config = {
        "routes": {
            "claude-router-main": {
                "strategy": "priority",
                "candidates": [
                    {"upstream": "proxypal", "model": "claude-3-5-sonnet-20241022"}
                ]
            }
        },
        "upstreams": {
            "proxypal": {
                "base_url": "https://api.proxypal.com",
                "auth": {"mode": "bearer", "token_env": "PROXYPAL_API_KEY"}
            }
        },
        "logging": {"level": "CRITICAL"}
    }
    router = SmartRouter(config, quota_reservations=reservations)

    # Mock the HTTP client to return a successful response (should not be reached if quota rejects)
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.content = b'{"result":"ok"}'
    mock_response.headers = {}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    monkeypatch.setattr(router, 'clients', {"proxypal": mock_client})

    # Act
    body = {"model": "claude-router-main", "messages": [{"role": "user", "content": "hello"}]}
    headers = {"authorization": "Bearer test-key"}
    response = await router.handle_messages(body, headers, "/v1/messages")

    # Assert: quota exhausted should return 503
    assert response.status_code == 503
    data = json.loads(response.body)
    assert "quota" in data["error"]["message"].lower()