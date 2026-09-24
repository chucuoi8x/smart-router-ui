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
