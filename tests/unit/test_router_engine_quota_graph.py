"""RouterEngine quota-graph contract tests.

The engine reads the local ``RuntimeQuotaIndex`` for sync admission; the async
authority is only used by ``select_candidates_async``.  These tests seed the
index directly to prove the contract without driving a thread bridge.
"""
import unittest

from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.quota import QuotaResource
from apps.gateway.quota.runtime_index import RuntimeQuotaIndex
from apps.gateway.routing.engine import RouterEngine
from apps.gateway.routing.models import ResourceCandidate, ResourceRef


class RouterEngineQuotaGraphTests(unittest.TestCase):
    def test_router_engine_uses_quota_graph_parent_chain_constraints(self):
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

        index = RuntimeQuotaIndex()
        index.replace_all(
            [
                QuotaResource("account", "account", "requests", 0, 60),
                QuotaResource("model:child-a", "model", "requests", 10, 60, parent_id="account"),
                QuotaResource("model:fallback", "model", "requests", 10, 60),
            ]
        )
        engine = RouterEngine(snapshot, quota_index=index)

        candidates = engine.select_candidates("chat")

        self.assertEqual([fallback.resource_ref], [candidate.resource_ref for candidate in candidates])

    def test_router_engine_uses_quota_graph_for_shared_group_deduplication(self):
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

        index = RuntimeQuotaIndex()
        index.replace_all(
            [
                QuotaResource("model:a", "model", "requests", 10, 60, shared_group_id="account:shared"),
                QuotaResource("model:b", "model", "requests", 10, 60, shared_group_id="account:shared"),
            ]
        )
        engine = RouterEngine(snapshot, quota_index=index)

        candidates = engine.select_candidates("chat")

        self.assertEqual([candidate.resource_ref], [selected.resource_ref for selected in candidates])
        self.assertEqual(["model:a"], candidates[0].metadata["quota_resource_ids"])


if __name__ == "__main__":
    unittest.main()
