"""Mandatory P0 E2E-04/E2E-05 tests."""
from __future__ import annotations

import asyncio

import pytest

fakeredis = pytest.importorskip("fakeredis")

from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaReservationRequest, QuotaResource
from apps.gateway.routing.engine import RedisCircuitRepository, RouterEngine
from apps.gateway.routing.models import ResourceCandidate, ResourceRef


@pytest.mark.asyncio
async def test_e2e04_concurrent_atomic_reservation_has_no_oversubscription():
    store = InMemoryQuotaReservations()
    store.add_resource(QuotaResource("tokens", "credential", "tokens", 100000, 60))

    async def reserve(name: str, amount: int):
        return await asyncio.to_thread(
            store.reserve_many,
            reservation_id=name,
            requests=[QuotaReservationRequest("tokens", amount)],
        )

    results = await asyncio.gather(
        reserve("r1", 40000), reserve("r2", 40000), reserve("r3", 35000)
    )

    assert [result.accepted for result in results].count(True) == 2
    assert [result.accepted for result in results].count(False) == 1
    assert store.snapshot("tokens").used == 80000
    assert store.snapshot("tokens").used <= 100000


def _snapshot() -> RuntimeConfigSnapshot:
    ref_a = ResourceRef("conn", "cred-a", "model")
    ref_b = ResourceRef("conn", "cred-b", "model")
    route = RouteConfig(
        route_name="chat",
        candidates=(
            ResourceCandidate(ref_a, "fake"),
            ResourceCandidate(ref_b, "fake"),
        ),
        fallback=(),
        strategy="priority",
    )
    return RuntimeConfigSnapshot(routes={"chat": route})


def test_e2e05_cross_instance_cooldown_hides_only_throttled_credential():
    redis = fakeredis.FakeRedis(decode_responses=True)
    engine_a = RouterEngine(_snapshot(), circuit_repository=RedisCircuitRepository(redis))
    engine_b = RouterEngine(_snapshot(), circuit_repository=RedisCircuitRepository(redis))
    throttled = ResourceRef("conn", "cred-a", "model")

    engine_a.circuit_repository.trip(throttled, cooldown_seconds=60)

    selected = engine_b.select_candidates("chat")
    assert [candidate.resource_ref.credential_scope for candidate in selected] == ["cred-b"]
