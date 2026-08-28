import unittest


class RouterEngineQuotaGraphTests(unittest.TestCase):
    def test_router_engine_uses_quota_graph_parent_chain_constraints(self):
        from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
        from apps.gateway.quota import InMemoryQuotaStore, QuotaResource
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.routing.models import ResourceCandidate, ResourceRef

        blocked = ResourceCandidate(
            ResourceRef("primary", "primary", "child-a"),
            driver_id="anthropic-compatible",
            metadata={"quota_resource_id": "model:child-a"},
        )
        fallback = ResourceCandidate(
            ResourceRef("backup", "backup", "fallback"),
            driver_id="anthropic-compatible",
            metadata={"quota_resource_id": "model:fallback"},
        )
        snapshot = RuntimeConfigSnapshot(
            routes={
                "chat": RouteConfig(
                    route_name="chat",
                    strategy="priority",
                    candidates=[blocked],
                    fallback=[fallback],
                )
            }
        )

        quota = InMemoryQuotaStore()
        engine = RouterEngine(snapshot, quota_reservations=quota)

        async def seed():
            await quota.add_resource(QuotaResource("account", "account", "requests", 0, 60))
            await quota.add_resource(QuotaResource("model:child-a", "model", "requests", 10, 60, parent_id="account"))
            await quota.add_resource(QuotaResource("model:fallback", "model", "requests", 10, 60))

        engine._run_coro_sync(seed())

        candidates = engine.select_candidates("chat")

        self.assertEqual([fallback.resource_ref], [candidate.resource_ref for candidate in candidates])

    def test_router_engine_uses_quota_graph_for_shared_group_deduplication(self):
        from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
        from apps.gateway.quota import InMemoryQuotaStore, QuotaResource
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.routing.models import ResourceCandidate, ResourceRef

        candidate = ResourceCandidate(
            ResourceRef("primary", "primary", "multi"),
            driver_id="anthropic-compatible",
            metadata={"quota_resource_ids": ["model:a", "model:b"]},
        )
        snapshot = RuntimeConfigSnapshot(
            routes={
                "chat": RouteConfig(
                    route_name="chat",
                    strategy="priority",
                    candidates=[candidate],
                )
            }
        )

        quota = InMemoryQuotaStore()
        engine = RouterEngine(snapshot, quota_reservations=quota)

        async def seed():
            await quota.add_resource(QuotaResource("model:a", "model", "requests", 10, 60, shared_group_id="account:shared"))
            await quota.add_resource(QuotaResource("model:b", "model", "requests", 10, 60, shared_group_id="account:shared"))

        engine._run_coro_sync(seed())

        candidates = engine.select_candidates("chat")

        self.assertEqual([candidate.resource_ref], [selected.resource_ref for selected in candidates])
        self.assertEqual(["model:a"], candidates[0].metadata["quota_resource_ids"])


if __name__ == "__main__":
    unittest.main()
