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


@pytest.mark.asyncio
async def test_quota_reservation_prevents_concurrent_exceedance(monkeypatch):
    """
    Test that quota reservation works in the live request flow:
    - First request consumes the only quota unit, succeeds.
    - Second request is rejected because quota is exhausted.
    """
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(
        QuotaResource(
            resource_id="model:chat",
            scope="model",
            metric="requests",
            limit=1,
            window_seconds=60,
            hard_limit=True,
        )
    )

    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "fast-model"}],
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
    monkeypatch.setenv("PRIMARY_TOKEN", "test-primary-token")

    # Mock upstream to succeed
    success_response = AsyncMock()
    success_response.status_code = 200
    success_response.content = b'{"result":"ok"}'
    success_response.headers = {}
    primary_client = AsyncMock()
    primary_client.post.return_value = success_response
    monkeypatch.setattr(router, "clients", {"primary": primary_client})

    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    headers = {"authorization": "Bearer test-key"}

    # First request should succeed and consume quota
    response1 = await router.handle_messages(body, headers, "/v1/messages")
    assert response1.status_code == 200

    # Verify quota used is 1
    resource = reservations.snapshot("model:chat")
    assert resource.used == 1

    # Second request should be rejected (quota exhausted)
    response2 = await router.handle_messages(body, headers, "/v1/messages")
    assert response2.status_code == 503
    data = json.loads(response2.body)
    assert "quota" in data["error"]["message"].lower()

    # Verify quota used is still 1 (reservation not released on success)
    resource = reservations.snapshot("model:chat")
    assert resource.used == 1

    # Verify the second request did not reach upstream
    # The mock client was called only once (for the first request)
    assert primary_client.post.call_count == 1


@pytest.mark.asyncio
async def test_live_request_records_ledger_entries(monkeypatch):
    """
    Test that InMemoryUsageLedger wired to SmartRouter creates
    RequestRecord on entry and AttemptRecord for every upstream attempt.
    """
    from apps.gateway.usage.ledger import InMemoryUsageLedger

    ledger = InMemoryUsageLedger()

    reservations = InMemoryQuotaReservations()

    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "fast-model"}],
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
    monkeypatch.setattr(router, "_usage_ledger", ledger)
    monkeypatch.setenv("PRIMARY_TOKEN", "test-primary-token")

    success_response = AsyncMock()
    success_response.status_code = 200
    success_response.content = b'{"result":"ok"}'
    success_response.headers = {}
    primary_client = AsyncMock()
    primary_client.post.return_value = success_response
    monkeypatch.setattr(router, "clients", {"primary": primary_client})

    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    headers = {"authorization": "Bearer test-key"}

    response = await router.handle_messages(body, headers, "/v1/messages")
    assert response.status_code == 200

    # A RequestRecord should exist with a generated request_id
    assert len(ledger._requests) >= 1
    request_id = next(iter(ledger._requests))
    req = ledger._requests[request_id]
    assert req.logical_model == "chat"

    # An AttemptRecord must exist keyed to the same request_id
    attempts = [a for a in ledger._attempts.values() if a.request_id == request_id]
    assert len(attempts) == 1
    assert attempts[0].provider_connection_id == "primary"
    assert attempts[0].status == "success"

    # A failed retry also creates an AttemptRecord
    fail_response = AsyncMock()
    fail_response.status_code = 503
    fail_response.content = b'{"error":"down"}'
    fail_response.headers = {}
    primary_client.post.return_value = fail_response

    response2 = await router.handle_messages(body, headers, "/v1/messages")
    assert response2.status_code == 503

    # Now there should be two requests total (one per handle_messages call)
    assert len(ledger._requests) == 2
    # And two attempts (each request attempted one candidate)
    assert len(ledger._attempts) == 2


@pytest.mark.asyncio
async def test_live_request_records_async_ledger_boundary(monkeypatch):
    """SmartRouter should await repository-style async ledger methods."""

    class FakeAsyncLedger:
        def __init__(self):
            self.requests = []
            self.attempts = []

        async def record_request(self, record, *, commit=False):
            self.requests.append(record)
            return record

        async def record_attempt(self, record, *, commit=False):
            self.attempts.append(record)
            return record

    ledger = FakeAsyncLedger()
    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "fast-model"}],
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

    router = SmartRouter(config, usage_ledger=ledger)
    monkeypatch.setenv("PRIMARY_TOKEN", "test-primary-token")

    success_response = AsyncMock()
    success_response.status_code = 200
    success_response.content = b'{"result":"ok"}'
    success_response.headers = {}
    primary_client = AsyncMock()
    primary_client.post.return_value = success_response
    monkeypatch.setattr(router, "clients", {"primary": primary_client})

    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {"authorization": "Bearer test-key"}, "/v1/messages")

    assert response.status_code == 200
    assert len(ledger.requests) == 1
    assert ledger.requests[0].logical_model == "chat"
    assert len(ledger.attempts) == 1
    assert ledger.attempts[0].request_id == ledger.requests[0].request_id
    assert ledger.attempts[0].provider_connection_id == "primary"
    assert ledger.attempts[0].model_resource_id == "fast-model"
    assert ledger.attempts[0].status == "success"


@pytest.mark.asyncio
async def test_rejected_requests_record_request_ledger_metadata(monkeypatch):
    """Quota rejections should create request ledger rows with reason metadata."""
    from apps.gateway.usage.ledger import InMemoryUsageLedger

    ledger = InMemoryUsageLedger()
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource(
        resource_id="model:chat",
        scope="tenant",
        metric="requests",
        limit=0,
        window_seconds=60,
        hard_limit=True,
    ))

    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "fast-model"}],
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
    router = SmartRouter(config, quota_reservations=reservations, usage_ledger=ledger)

    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {}, "/v1/messages")

    assert response.status_code == 503
    assert len(ledger._requests) == 1
    req = next(iter(ledger._requests.values()))
    assert req.metadata["status"] == "rejected"
    assert req.metadata["reason"] == "quota_exhausted"


@pytest.mark.asyncio
async def test_upstream_503_attempt_status_uses_error_classifier(monkeypatch):
    """Transient provider failures should persist normalized attempt status."""
    from apps.gateway.usage.ledger import InMemoryUsageLedger

    ledger = InMemoryUsageLedger()
    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "fast-model"}],
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
    router = SmartRouter(config, usage_ledger=ledger)
    monkeypatch.setenv("PRIMARY_TOKEN", "test-primary-token")

    fail_response = AsyncMock()
    fail_response.status_code = 503
    fail_response.content = b'{"error":{"message":"down"}}'
    fail_response.headers = {}
    primary_client = AsyncMock()
    primary_client.post.return_value = fail_response
    monkeypatch.setattr(router, "clients", {"primary": primary_client})

    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {"authorization": "Bearer test-key"}, "/v1/messages")

    assert response.status_code == 503
    attempt = next(iter(ledger._attempts.values()))
    assert attempt.status == "TRANSIENT_NETWORK"


@pytest.mark.asyncio
async def test_upstream_quota_429_attempt_status_is_quota_exhausted(monkeypatch):
    """Provider quota exhaustion should not be flattened into RATE_LIMIT."""
    from apps.gateway.usage.ledger import InMemoryUsageLedger

    ledger = InMemoryUsageLedger()
    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "fast-model"}],
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
    router = SmartRouter(config, usage_ledger=ledger)
    monkeypatch.setenv("PRIMARY_TOKEN", "test-primary-token")

    quota_response = AsyncMock()
    quota_response.status_code = 429
    quota_response.content = b'{"error":{"type":"insufficient_quota","message":"Monthly quota exhausted"}}'
    quota_response.headers = {"X-Quota-Reset": "2026-09-01T00:00:00Z"}
    primary_client = AsyncMock()
    primary_client.post.return_value = quota_response
    monkeypatch.setattr(router, "clients", {"primary": primary_client})

    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {"authorization": "Bearer test-key"}, "/v1/messages")

    assert response.status_code == 429
    attempt = next(iter(ledger._attempts.values()))
    assert attempt.status == "QUOTA_EXHAUSTED"


@pytest.mark.asyncio
async def test_upstream_quota_exhaustion_updates_candidate_quota_resource(monkeypatch):
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("account:primary", "account", "requests", 5, 60))

    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "fast-model", "quota_resource_id": "account:primary"}],
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
    monkeypatch.setenv("PRIMARY_TOKEN", "test-primary-token")

    quota_response = AsyncMock()
    quota_response.status_code = 429
    quota_response.content = b'{"error":{"type":"insufficient_quota","message":"Monthly quota exhausted"}}'
    quota_response.headers = {"X-Quota-Reset": "2026-09-01T00:00:00Z"}
    primary_client = AsyncMock()
    primary_client.post.return_value = quota_response
    monkeypatch.setattr(router, "clients", {"primary": primary_client})

    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {"authorization": "Bearer test-key"}, "/v1/messages")

    assert response.status_code == 429
    observed = await router.quota_reservations.snapshot("account:primary")
    assert observed.used == observed.limit
    assert observed.effective_remaining == 0
    assert observed.source == "provider_error"
    assert observed.confidence == "inferred"


@pytest.mark.asyncio
async def test_upstream_quota_exhaustion_excludes_candidate_on_next_request(monkeypatch):
    reservations = InMemoryQuotaReservations()
    reservations.add_resource(QuotaResource("account:primary", "account", "requests", 5, 60))
    reservations.add_resource(QuotaResource("model:fallback-model", "model", "requests", 10, 60))

    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "fast-model", "quota_resource_id": "account:primary"}],
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

    quota_response = AsyncMock()
    quota_response.status_code = 429
    quota_response.content = b'{"error":{"type":"insufficient_quota","message":"Monthly quota exhausted"}}'
    quota_response.headers = {}
    success_response = AsyncMock()
    success_response.status_code = 200
    success_response.content = b'{"result":"fallback"}'
    success_response.headers = {}
    primary_client = AsyncMock()
    primary_client.post.return_value = quota_response
    backup_client = AsyncMock()
    backup_client.post.return_value = success_response
    monkeypatch.setattr(router, "clients", {"primary": primary_client, "backup": backup_client})

    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    headers = {"authorization": "Bearer test-key"}
    response1 = await router.handle_messages(body, headers, "/v1/messages")
    response2 = await router.handle_messages(body, headers, "/v1/messages")

    assert response1.status_code == 200
    assert response2.status_code == 200
    assert primary_client.post.call_count == 1
    assert backup_client.post.call_count == 2


@pytest.mark.asyncio
async def test_upstream_rate_limit_uses_retry_after_and_uses_fallback(monkeypatch):
    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "fast-model"}],
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
    router = SmartRouter(config)
    monkeypatch.setenv("PRIMARY_TOKEN", "test-primary-token")
    monkeypatch.setenv("BACKUP_TOKEN", "test-backup-token")

    rate_limit_response = AsyncMock()
    rate_limit_response.status_code = 429
    rate_limit_response.content = b'{"error":{"type":"rate_limit_error","message":"Too many requests"}}'
    rate_limit_response.headers = {"Retry-After": "33"}
    success_response = AsyncMock()
    success_response.status_code = 200
    success_response.content = b'{"result":"fallback"}'
    success_response.headers = {}
    primary_client = AsyncMock()
    primary_client.post.return_value = rate_limit_response
    backup_client = AsyncMock()
    backup_client.post.return_value = success_response
    monkeypatch.setattr(router, "clients", {"primary": primary_client, "backup": backup_client})

    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {"authorization": "Bearer test-key"}, "/v1/messages")

    assert response.status_code == 200
    primary_client.post.assert_called_once()
    backup_client.post.assert_called_once()
    remaining = router.circuits["primary:fast-model"].cooldown_until - asyncio.get_running_loop().time()
    assert remaining > 25
    assert router.circuits["primary:fast-model"].last_kind == "RATE_LIMIT"


@pytest.mark.asyncio
async def test_request_scoped_error_does_not_use_fallback(monkeypatch):
    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "fast-model"}],
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
    router = SmartRouter(config)
    monkeypatch.setenv("PRIMARY_TOKEN", "test-primary-token")
    monkeypatch.setenv("BACKUP_TOKEN", "test-backup-token")

    invalid_response = AsyncMock()
    invalid_response.status_code = 400
    invalid_response.content = b'{"error":{"message":"bad schema"}}'
    invalid_response.headers = {}
    primary_client = AsyncMock()
    primary_client.post.return_value = invalid_response
    backup_client = AsyncMock()
    monkeypatch.setattr(router, "clients", {"primary": primary_client, "backup": backup_client})

    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    response = await router.handle_messages(body, {"authorization": "Bearer test-key"}, "/v1/messages")

    assert response.status_code == 400
    backup_client.post.assert_not_called()
    assert "primary:fast-model" not in router.circuits
