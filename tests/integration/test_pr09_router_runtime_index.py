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

    async def list_resources(self):
        self.list_calls += 1
        raise AssertionError("Redis SCAN/list_resources called on request path")

    async def check_many(self, requests):
        self.check_calls += 1
        raise AssertionError("local eligibility check should use RuntimeQuotaIndex")


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
