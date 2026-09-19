"""PR-09 RouterEngine uses local index, never Redis list/SCAN on request path."""
import pytest

from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.quota.reservations import QuotaResource
from apps.gateway.quota.runtime_index import RuntimeQuotaIndex
from apps.gateway.routing.engine import RouterEngine
from apps.gateway.routing.models import ResourceCandidate, ResourceRef


class RedisAuthoritySpy:
    """Mutation authority whose list_resources must never run on request path."""

    def __init__(self):
        self.list_calls = 0
        self.check_calls = 0
        self.snapshot_calls = 0

    async def list_resources(self):
        self.list_calls += 1
        raise AssertionError("Redis SCAN/list_resources called on request path")

    async def snapshot(self, resource_id: str) -> QuotaResource:
        self.snapshot_calls += 1
        return QuotaResource(resource_id, "credential", "requests", 100, 60, used=10)

    async def check_many(self, requests):
        self.check_calls += 1
        from apps.gateway.quota.reservations import QuotaAdmissionResult

        return QuotaAdmissionResult(
            accepted=True,
            hard_failures={},
            soft_pressure_by_resource={},
            remaining_by_resource={r.resource_id: 5 for r in requests},
        )


@pytest.mark.asyncio
async def test_router_engine_uses_local_index_without_redis_scan():
    candidate = ResourceCandidate(
        resource_ref=ResourceRef("conn-a", "cred-a", "model-a"),
        driver_id="generic-openai",
        metadata={"quota_resource_ids": ["rpm:cred-a"]},
    )
    snapshot = RuntimeConfigSnapshot(
        routes={"main": RouteConfig("main", "priority", candidates=[candidate])}
    )
    index = RuntimeQuotaIndex()
    index.replace_all([
        QuotaResource("rpm:cred-a", "credential:cred-a", "requests", 100, 60, used=10),
    ])
    authority = RedisAuthoritySpy()
    engine = RouterEngine(snapshot, quota_reservations=authority, quota_index=index)

    ranked = await engine._quota_rank([candidate])

    assert len(ranked) == 1
    assert ranked[0].resource_ref.key == candidate.resource_ref.key
    assert authority.list_calls == 0
    assert authority.check_calls == 0


@pytest.mark.asyncio
async def test_router_engine_never_scans_async_authority_when_index_unloaded():
    """P0-08: an unloaded local index must not trigger a quota: * SCAN.

    Startup/background refresh owns index hydration; the request path may fall
    back to targeted async check_many on the authority, but never to
    list_resources, regardless of index state.
    """
    candidate = ResourceCandidate(
        resource_ref=ResourceRef("conn-a", "cred-a", "model-a"),
        driver_id="generic-openai",
        metadata={"quota_resource_ids": ["rpm:cred-a"]},
    )
    snapshot = RuntimeConfigSnapshot(
        routes={"main": RouteConfig("main", "priority", candidates=[candidate])}
    )
    authority = RedisAuthoritySpy()
    engine = RouterEngine(snapshot, quota_reservations=authority)  # index NOT loaded

    ranked = await engine._quota_rank([candidate])

    assert len(ranked) == 1
    assert authority.list_calls == 0, "request path must not SCAN the quota authority"
    assert authority.check_calls == 1

