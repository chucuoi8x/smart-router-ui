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


def test_smart_router_preserves_candidate_quota_metadata():
    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [
                    {
                        "upstream": "primary",
                        "model": "fast-model",
                        "quota_resource_id": "account:primary",
                        "quota_resource_ids": ["account:primary", "model:fast-model"],
                    }
                ],
                "fallback": [
                    {
                        "upstream": "backup",
                        "model": "fallback-model",
                        "quota_resource_id": None,
                        "quota_resource_ids": "malformed-scalar",
                    }
                ],
            }
        },
        "upstreams": {
            "primary": {
                "base_url": "https://primary.example",
                "auth": {"mode": "bearer", "token_env": "PRIMARY_TOKEN"},
            },
            "backup": {
                "base_url": "https://backup.example",
                "auth": {"mode": "bearer", "token_env": "BACKUP_TOKEN"},
            },
        },
        "logging": {"level": "CRITICAL"},
    }

    router = SmartRouter(config)

    primary = router.routes["chat"]["candidates"][0]
    fallback = router.routes["chat"]["fallback"][0]
    assert primary.metadata["quota_resource_id"] == "account:primary"
    assert primary.metadata["quota_resource_ids"] == ["account:primary", "model:fast-model"]
    assert fallback.metadata["quota_resource_id"] is None
    assert fallback.metadata["quota_resource_ids"] == "malformed-scalar"


@pytest.mark.asyncio
async def test_candidate_quota_exhaustion_skips_primary_and_uses_fallback(monkeypatch):
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("model:primary-model", "model", "requests", 0, 60))
    reservations.add_resource(QuotaResource("model:fallback-model", "model", "requests", 10, 60))

    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "primary-model"}],
                "fallback": [{"upstream": "backup", "model": "fallback-model"}],
            }
        },
        "upstreams": {
            "primary": {
                "base_url": "https://primary.example",
                "auth": {"mode": "bearer", "token_env": "PRIMARY_TOKEN"},
            },
            "backup": {
                "base_url": "https://backup.example",
                "auth": {"mode": "bearer", "token_env": "BACKUP_TOKEN"},
            },
        },
        "logging": {"level": "CRITICAL"},
    }
    router = SmartRouter(config, quota_reservations=reservations)
    monkeypatch.setenv("PRIMARY_TOKEN", "test-primary-token")
    monkeypatch.setenv("BACKUP_TOKEN", "test-backup-token")

    primary_client = AsyncMock()
    backup_response = AsyncMock()
    backup_response.status_code = 200
    backup_response.content = b'{"result":"fallback"}'
    backup_response.headers = {}
    backup_client = AsyncMock()
    backup_client.post.return_value = backup_response
    monkeypatch.setattr(router, "clients", {"primary": primary_client, "backup": backup_client})

    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {"authorization": "Bearer test-key"}, "/v1/messages")

    assert response.status_code == 200
    assert response.body == b'{"result":"fallback"}'
    primary_client.post.assert_not_called()
    backup_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_candidate_quota_exhaustion_all_candidates_returns_overloaded(monkeypatch):
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("model:primary-model", "model", "requests", 0, 60))

    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "primary-model"}],
            }
        },
        "upstreams": {
            "primary": {
                "base_url": "https://primary.example",
                "auth": {"mode": "bearer", "token_env": "PRIMARY_TOKEN"},
            },
        },
        "logging": {"level": "CRITICAL"},
    }
    router = SmartRouter(config, quota_reservations=reservations)
    primary_client = AsyncMock()
    monkeypatch.setattr(router, "clients", {"primary": primary_client})

    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {"authorization": "Bearer test-key"}, "/v1/messages")

    assert response.status_code == 503
    data = json.loads(response.body)
    assert data["error"]["type"] == "overloaded"
    primary_client.post.assert_not_called()
