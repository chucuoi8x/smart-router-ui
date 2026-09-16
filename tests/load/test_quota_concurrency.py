"""Load tests Step 133 — concurrent quota admission hardening (M7/AC-06)."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaReservationRequest, QuotaResource


@pytest.mark.asyncio
async def test_concurrent_reservations_never_oversubscribe_hard_limit():
    store = InMemoryQuotaReservations()
    store.add_resource(QuotaResource(resource_id="load-rid", scope="test", metric="requests", limit=25, window_seconds=60))

    async def attempt(i):
        return await asyncio.to_thread(store.reserve, resource_id="load-rid", amount=1, reservation_id=f"load-{i}")

    results = await asyncio.gather(*(attempt(i) for i in range(100)))
    accepted = [r for r in results if r.accepted]
    rejected = [r for r in results if not r.accepted]
    assert len(accepted) == 25
    assert len(rejected) == 75
    state = store.snapshot("load-rid")
    assert state.used == 25
    assert state.used <= state.limit


@pytest.mark.asyncio
async def test_parallel_multi_resource_reservation_is_atomic():
    store = InMemoryQuotaReservations()
    store.add_resource(QuotaResource(resource_id="req", scope="test", metric="requests", limit=10, window_seconds=60))
    store.add_resource(QuotaResource(resource_id="tok", scope="test", metric="tokens", limit=100, window_seconds=60))

    async def attempt(i):
        return await asyncio.to_thread(
            store.reserve_many,
            reservation_id=f"atomic-{i}",
            requests=[QuotaReservationRequest("req", 1), QuotaReservationRequest("tok", 20)],
        )

    results = await asyncio.gather(*(attempt(i) for i in range(20)))
    accepted = [r for r in results if r.accepted]
    assert len(accepted) == 5
    assert store.snapshot("req").used == 5
    assert store.snapshot("tok").used == 100
