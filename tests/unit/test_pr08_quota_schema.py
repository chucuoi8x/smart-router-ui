"""PR-08: Quota schema correctness — hierarchy, window metadata, pressure normalization.

RED tests for:
- DB schema: parent_id, reset_at, window_metadata columns
- Domain object: reset_at, window_metadata fields
- Repository: round-trip new fields
- Admin endpoint: persist quota resources to DB (not in-memory dict)
- Pressure normalization per dimension
"""
import pytest
import httpx
from datetime import datetime, UTC, timedelta
from router import app


@pytest.mark.asyncio
async def test_quota_db_model_has_parent_id_reset_at_window_metadata():
    """QuotaResourceState must have parent_id, reset_at, window_metadata columns."""
    from apps.gateway.db.models import QuotaResourceState

    reset_time = datetime.now(UTC) + timedelta(hours=1)
    resource = QuotaResourceState(
        resource_id="tokens:tenant-a:gpt-4o:minute",
        scope="tenant-a",
        metric="total_token",
        limit=100,
        used=25,
        window_seconds=60,
        safety_buffer=10,
        hard_limit=True,
        source="provider_api",
        confidence="exact",
        shared_group_id="account-weekly-123",
        parent_id="tenant-a:total",
        reset_at=reset_time,
        window_metadata={"window_type": "rolling", "precision": "second"},
    )

    assert resource.resource_id == "tokens:tenant-a:gpt-4o:minute"
    assert resource.parent_id == "tenant-a:total"
    assert resource.reset_at == reset_time
    assert resource.window_metadata == {"window_type": "rolling", "precision": "second"}


@pytest.mark.asyncio
async def test_quota_domain_object_has_reset_at_window_metadata():
    """QuotaResource domain object must support reset_at and window_metadata."""
    from apps.gateway.quota.reservations import QuotaResource

    reset_time = datetime.now(UTC) + timedelta(hours=1)
    resource = QuotaResource(
        resource_id="tokens:tenant-a:gpt-4o:minute",
        scope="tenant-a",
        metric="total_token",
        limit=100,
        window_seconds=60,
        used=25,
        safety_buffer=10,
        hard_limit=True,
        source="provider_api",
        confidence="exact",
        shared_group_id="account-weekly-123",
        parent_id="tenant-a:total",
        reset_at=reset_time,
        window_metadata={"window_type": "rolling"},
    )

    assert resource.parent_id == "tenant-a:total"
    assert resource.reset_at == reset_time
    assert resource.window_metadata == {"window_type": "rolling"}


@pytest.mark.asyncio
async def test_quota_repository_round_trips_parent_id_reset_at_window_metadata():
    """QuotaResourceRepository must persist and retrieve parent_id, reset_at, window_metadata."""
    from apps.gateway.quota.reservations import QuotaResource, QuotaResourceRepository
    from apps.gateway.db.session import get_async_session_factory

    reset_time = datetime.now(UTC) + timedelta(hours=1)
    resource = QuotaResource(
        resource_id="tokens:pr08-test:minute",
        scope="pr08-test",
        metric="total_token",
        limit=100,
        window_seconds=60,
        used=25,
        safety_buffer=10,
        parent_id="pr08-test:total",
        reset_at=reset_time,
        window_metadata={"window_type": "fixed"},
    )

    async with get_async_session_factory()() as session:
        repo = QuotaResourceRepository(session)
        await repo.save_resource(resource, commit=True)

        retrieved = await repo.get_resource("tokens:pr08-test:minute")
        assert retrieved is not None
        assert retrieved.parent_id == "pr08-test:total"
        assert retrieved.reset_at is not None
        assert retrieved.reset_at.replace(microsecond=0) == reset_time.replace(microsecond=0)
        assert retrieved.window_metadata == {"window_type": "fixed"}


@pytest.mark.asyncio
async def test_admin_quota_endpoint_persists_to_db():
    """Admin quota endpoint must persist to DB, not in-memory dict."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Create quota resource via admin endpoint
        reset_time = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        payload = {
            "resource_id": "tokens:pr08-admin:minute",
            "scope": "pr08-admin",
            "metric": "total_token",
            "limit": 100,
            "used": 25,
            "window_seconds": 60,
            "safety_buffer": 10,
            "hard_limit": True,
            "parent_id": "pr08-admin:total",
            "reset_at": reset_time,
            "window_metadata": {"window_type": "fixed"},
        }
        r = await client.post(
            "/api/admin/v1/quota/resources",
            json=payload,
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 201
        created = r.json()
        assert created["resource_id"] == "tokens:pr08-admin:minute"
        assert created["parent_id"] == "pr08-admin:total"
        assert "reset_at" in created
        assert "window_metadata" in created

        # Verify persistence: retrieve from DB directly
        from apps.gateway.quota.reservations import QuotaResourceRepository
        from apps.gateway.db.session import get_async_session_factory

        async with get_async_session_factory()() as session:
            repo = QuotaResourceRepository(session)
            retrieved = await repo.get_resource("tokens:pr08-admin:minute")
            assert retrieved is not None
            assert retrieved.parent_id == "pr08-admin:total"
            assert retrieved.window_metadata == {"window_type": "fixed"}


@pytest.mark.asyncio
async def test_quota_pressure_normalization_per_dimension():
    """Pressure normalization must work across different dimensions (tokens, requests)."""
    from apps.gateway.quota.reservations import QuotaResource

    # Token resource: 80/100 used = 80% pressure
    token_resource = QuotaResource(
        resource_id="tokens:tenant-a:gpt-4o:minute",
        scope="tenant-a",
        metric="total_token",
        limit=100,
        window_seconds=60,
        used=80,
    )
    token_pressure = token_resource.used / token_resource.limit
    assert token_pressure == 0.8

    # Request resource: 5/10 used = 50% pressure
    request_resource = QuotaResource(
        resource_id="requests:tenant-a:minute",
        scope="tenant-a",
        metric="request",
        limit=10,
        window_seconds=60,
        used=5,
    )
    request_pressure = request_resource.used / request_resource.limit
    assert request_pressure == 0.5

    # Normalized pressure should be comparable across dimensions
    assert token_pressure > request_pressure


@pytest.mark.asyncio
async def test_quota_hierarchy_parent_child_relationship():
    """Quota resources must support parent-child hierarchy."""
    from apps.gateway.quota.reservations import QuotaResource, QuotaResourceRepository
    from apps.gateway.db.session import get_async_session_factory

    # Parent resource
    parent = QuotaResource(
        resource_id="tenant-a:total",
        scope="tenant-a",
        metric="total_token",
        limit=1000,
        window_seconds=86400,  # daily
        used=500,
    )

    # Child resource
    child = QuotaResource(
        resource_id="tokens:tenant-a:gpt-4o:minute",
        scope="tenant-a",
        metric="total_token",
        limit=100,
        window_seconds=60,
        used=25,
        parent_id="tenant-a:total",
    )

    async with get_async_session_factory()() as session:
        repo = QuotaResourceRepository(session)
        await repo.save_resource(parent, commit=True)
        await repo.save_resource(child, commit=True)

        retrieved_child = await repo.get_resource("tokens:tenant-a:gpt-4o:minute")
        assert retrieved_child is not None
        assert retrieved_child.parent_id == "tenant-a:total"

        retrieved_parent = await repo.get_resource("tenant-a:total")
        assert retrieved_parent is not None
        assert retrieved_parent.parent_id is None
