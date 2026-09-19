"""Production SmartRouter quota index lifecycle, not only engine injection."""
import pytest

from router import SmartRouter
from apps.gateway.quota.adapter import AsyncQuotaFacade
from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource


def config():
    return {"routes": {"main": {"candidates": [
        {"upstream": "one", "model": "m", "quota_resource_id": "rpm:a"}
    ]}}, "upstreams": {}}


@pytest.mark.asyncio
async def test_start_loads_index_and_ranking_never_lists(monkeypatch):
    backend = AsyncQuotaFacade(InMemoryQuotaReservations())
    await backend.add_resource(QuotaResource("rpm:a", "credential:a", "requests", 10, 60, used=10))
    service = SmartRouter(config(), quota_reservations=backend)
    async def no_hydrate():
        pass
    monkeypatch.setattr(service, "_hydrate_quota_from_db", no_hydrate)
    await service.start()
    try:
        assert service.quota_index.loaded
        async def no_scan():
            pytest.fail("request ranking scanned quota store")
        monkeypatch.setattr(backend, "list_resources", no_scan)
        assert await service._candidate_order("main") == []
    finally:
        await service.close()
