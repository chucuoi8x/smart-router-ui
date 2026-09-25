"""Final convergence: async admission and local-only scoring."""
import inspect

import pytest

from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.quota.adapter import AsyncQuotaFacade
from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
from apps.gateway.quota.runtime_index import RuntimeQuotaIndex
from apps.gateway.routing.engine import RouterEngine
from apps.gateway.routing.models import ResourceCandidate, ResourceRef
from apps.gateway.routing.scoring import ScoringConfig


def test_router_engine_contains_no_thread_join_sync_bridge():
    source = inspect.getsource(RouterEngine)
    assert "thread.join" not in source
    assert "threading.Thread" not in source
    assert "_run_coro_sync" not in source


def test_routing_module_has_no_thread_bridge_anywhere():
    """P0-20 §7.1: the whole routing module must stay free of sync thread hops.

    The RouterEngine class-source check alone hid a module-level
    ``_run_coro_sync`` helper that ``RedisCircuitRepository`` still used to
    drive async clients from sync code — a thread join reachable from any
    sync admission call.
    """
    import apps.gateway.routing.engine as engine_module

    source = inspect.getsource(engine_module)
    assert "thread.join" not in source
    assert "threading.Thread" not in source
    assert "_run_coro_sync" not in source


@pytest.mark.asyncio
async def test_sync_circuit_entry_points_reject_async_clients_without_threads():
    """An async Redis client must be used via the async API, never thread-hopped."""
    import fakeredis

    from apps.gateway.routing.engine import RedisCircuitRepository

    authority = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisCircuitRepository(authority)
    ref = ResourceRef("conn", "cred-a", "model")
    await repo.trip_async(ref, cooldown_seconds=30)
    with pytest.raises(RuntimeError, match="async"):
        repo.is_available(ref)
    with pytest.raises(RuntimeError, match="async"):
        repo.trip(ref, cooldown_seconds=30)


@pytest.mark.asyncio
async def test_scoring_reads_index_not_async_authority(monkeypatch):
    resource = QuotaResource("model:m", "credential:c", "requests", 100, 60)
    store = InMemoryQuotaReservations()
    store.add_resource(resource)
    authority = AsyncQuotaFacade(store)
    index = RuntimeQuotaIndex()
    index.replace_all([resource])
    candidate = ResourceCandidate(ResourceRef("conn", "c", "m"), "generic-openai")
    snapshot = RuntimeConfigSnapshot(routes={"main": RouteConfig("main", "priority", [candidate])})
    engine = RouterEngine(snapshot, quota_reservations=authority, quota_index=index,
                          scoring_config=ScoringConfig(enabled=True, mode="active"))
    calls = []
    async def forbidden(resource_id):
        calls.append(resource_id)
        return resource
    monkeypatch.setattr(authority, "snapshot", forbidden)
    assert len(await engine.select_candidates_async("main")) == 1
    assert calls == [], "normal scoring fetched Redis rather than published local index"
