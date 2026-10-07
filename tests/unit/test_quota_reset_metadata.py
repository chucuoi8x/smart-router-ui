"""Database-to-Redis-to-runtime metadata contract, independent clients."""
from datetime import datetime, timezone

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.gateway.db.models import Base
from apps.gateway.quota.redis_backend import RedisQuotaStore
from apps.gateway.quota.reservations import QuotaResource, QuotaResourceRepository
from apps.gateway.quota.runtime_index import RuntimeQuotaIndex


@pytest.mark.asyncio
async def test_reset_metadata_survives_db_redis_and_independent_runtime_restart():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    server = FakeServer()
    writer = RedisQuotaStore()
    reader = RedisQuotaStore()
    writer._r = FakeAsyncRedis(server=server, decode_responses=True)
    reader._r = FakeAsyncRedis(server=server, decode_responses=True)
    assert writer._r is not reader._r
    resource = QuotaResource(
        "quota:child", "credential", "total_tokens", 1000, 60,
        used=200, safety_buffer=10, hard_limit=True,
        source="provider_api", confidence="exact", parent_id="parent-1",
        shared_group_id="group-1",
        reset_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
        window_metadata={"reset_policy": "fixed_window", "nested": {"zones": ["UTC"]}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            repo = QuotaResourceRepository(session)
            await repo.save_resource(resource, commit=True)
        async with factory() as session:
            persisted = await QuotaResourceRepository(session).get_resource(resource.resource_id)
            await writer.add_resource(persisted)
        raw = await reader._r.hgetall("quota:quota:child")
        assert raw["reset_at"] == resource.reset_at.isoformat()
        assert "window_metadata" in raw
        for _ in range(2):
            index = RuntimeQuotaIndex()
            index.replace_all(await reader.list_resources())
            assert index.snapshot(resource.resource_id) == resource
            assert index.snapshot(resource.resource_id).remaining == 800
            copy = index.snapshot(resource.resource_id)
            copy.window_metadata["nested"]["zones"].append("changed")
            assert index.snapshot(resource.resource_id) == resource
    finally:
        await writer._r.aclose()
        await reader._r.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_redis_quota_without_metadata_remains_readable():
    store = RedisQuotaStore()
    store._r = FakeAsyncRedis(server=FakeServer(), decode_responses=True)
    try:
        await store._r.hset("quota:old", mapping={
            "scope": "model", "metric": "requests", "limit": "10", "window_seconds": "60",
        })
        resource = await store.snapshot("old")
        assert resource.reset_at is None
        assert resource.window_metadata == {}
    finally:
        await store._r.aclose()
