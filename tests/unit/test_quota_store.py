import pytest

from apps.gateway.quota import (
    InMemoryQuotaStore,
    QuotaGraph,
    QuotaReservationRequest,
    QuotaResource,
)


@pytest.mark.asyncio
async def test_in_memory_quota_store_parent_chain_limits_siblings_all_or_nothing():
    store = InMemoryQuotaStore()
    await store.add_resource(QuotaResource("account", "account", "requests", 1, 60))
    await store.add_resource(QuotaResource("model:a", "model", "requests", 10, 60, parent_id="account"))
    await store.add_resource(QuotaResource("model:b", "model", "requests", 10, 60, parent_id="account"))

    rejected = await store.reserve_many(
        reservation_id="parent-batch",
        requests=[
            QuotaReservationRequest("model:a", 1),
            QuotaReservationRequest("model:b", 1),
        ],
    )

    assert rejected.accepted is False
    assert rejected.reason == "quota_exceeded"
    assert rejected.effective_remaining_by_resource == {"model:a": 1, "model:b": 1}
    assert (await store.snapshot("model:a")).used == 0
    assert (await store.snapshot("model:b")).used == 0
    assert (await store.snapshot("account")).used == 0


@pytest.mark.asyncio
async def test_in_memory_quota_store_parent_usage_is_reserved_and_released():
    store = InMemoryQuotaStore()
    await store.add_resource(QuotaResource("account", "account", "requests", 2, 60))
    await store.add_resource(QuotaResource("model:a", "model", "requests", 10, 60, parent_id="account"))

    accepted = await store.reserve(resource_id="model:a", amount=1, reservation_id="parent-res")
    assert accepted.accepted is True
    assert (await store.snapshot("account")).used == 1
    assert await store.release("parent-res") is True
    assert (await store.snapshot("account")).used == 0
    assert (await store.snapshot("model:a")).used == 0


@pytest.mark.asyncio
async def test_quota_graph_effective_remaining_uses_parent_and_group_minimums():
    store = InMemoryQuotaStore()
    await store.add_resource(QuotaResource("account", "account", "requests", 50, 60, used=40))
    await store.add_resource(QuotaResource("model:a", "model", "requests", 100, 60, shared_group_id="shared", parent_id="account"))
    await store.add_resource(QuotaResource("model:b", "model", "requests", 15, 60, used=3, shared_group_id="shared"))

    graph = QuotaGraph(store)
    await graph.load()

    assert graph.get_parent_chain("model:a") == ["account"]
    assert set(graph.get_group_peers("model:a")) == {"model:a", "model:b"}
    assert graph.get_effective_remaining("model:a") == 10
    admission = graph.check_many([QuotaReservationRequest("model:a", 11)])
    assert admission.accepted is False
    assert admission.hard_failures == {"model:a": 1}
