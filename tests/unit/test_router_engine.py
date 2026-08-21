import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

class RouterEngineTests(unittest.TestCase):
    def test_legacy_compiler_parses_yaml_correctly(self):
        from apps.gateway.config.compiler import LegacyConfigCompiler
        from apps.gateway.config.snapshot import RuntimeConfigSnapshot

        # We will compile the actual config.yaml to verify compatibility
        config_path = Path(__file__).resolve().parents[2] / 'config.yaml'
        self.assertTrue(config_path.exists())

        compiler = LegacyConfigCompiler()
        snapshot = compiler.compile_file(config_path)

        self.assertIsInstance(snapshot, RuntimeConfigSnapshot)

        # Verify logical routes exist
        self.assertIn('claude-router-main', snapshot.routes)
        self.assertIn('claude-router-fast', snapshot.routes)

        # Verify connections compiled
        self.assertIn('proxypal', snapshot.connections)
        self.assertIn('aibox', snapshot.connections)

        # Verify credential mappings
        proxypal_conn = snapshot.connections['proxypal']
        self.assertEqual(proxypal_conn.auth_mode, 'bearer')
        self.assertEqual(proxypal_conn.token_env, 'PROXYPAL_API_KEY')

    def test_router_engine_resolve_route_priority_strategy(self):
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.config.compiler import LegacyConfigCompiler

        config_path = Path(__file__).resolve().parents[2] / 'config.yaml'
        compiler = LegacyConfigCompiler()
        snapshot = compiler.compile_file(config_path)

        engine = RouterEngine(snapshot)
        candidates = engine.resolve_route('claude-router-main')

        # Verify priority is preserved (proxypal candidates first, then fallback)
        self.assertGreater(len(candidates), 0)
        self.assertEqual(candidates[0].resource_ref.provider_connection_id, 'proxypal')
        self.assertEqual(candidates[-1].resource_ref.provider_connection_id, 'aibox')

    def test_router_engine_respects_circuit_breakers(self):
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.config.compiler import LegacyConfigCompiler
        from apps.gateway.routing.models import ResourceRef

        config_path = Path(__file__).resolve().parents[2] / 'config.yaml'
        compiler = LegacyConfigCompiler()
        snapshot = compiler.compile_file(config_path)

        engine = RouterEngine(snapshot)

        # Initially all should be available
        candidates = engine.select_candidates('claude-router-main')
        initial_len = len(candidates)

        # Trip the circuit for the first candidate
        ref = candidates[0].resource_ref
        engine.circuit_repository.trip(ref, cooldown_seconds=60)

        # Get candidates again - the tripped one should be excluded
        new_candidates = engine.select_candidates('claude-router-main')
        self.assertEqual(len(new_candidates), initial_len - 1)
        self.assertNotIn(ref, [c.resource_ref for c in new_candidates])

    def test_router_engine_filters_hard_quota_exhausted_candidates(self):
        from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.routing.models import ResourceCandidate, ResourceRef

        exhausted = ResourceCandidate(
            ResourceRef("primary", "primary", "primary-model"),
            driver_id="anthropic-compatible",
        )
        available = ResourceCandidate(
            ResourceRef("secondary", "secondary", "secondary-model"),
            driver_id="anthropic-compatible",
        )
        snapshot = RuntimeConfigSnapshot(
            routes={
                "chat": RouteConfig(
                    route_name="chat",
                    strategy="priority",
                    candidates=[exhausted, available],
                )
            }
        )
        quota = InMemoryQuotaReservations()
        quota.add_resource(
            QuotaResource(
                resource_id="model:primary-model",
                scope="model",
                metric="requests",
                limit=0,
                window_seconds=60,
                hard_limit=True,
            )
        )
        quota.add_resource(
            QuotaResource(
                resource_id="model:secondary-model",
                scope="model",
                metric="requests",
                limit=10,
                window_seconds=60,
                hard_limit=True,
            )
        )

        engine = RouterEngine(snapshot, quota_reservations=quota)
        candidates = engine.select_candidates("chat")

        self.assertEqual([available.resource_ref], [candidate.resource_ref for candidate in candidates])

    def test_router_engine_uses_candidate_quota_resource_metadata(self):
        from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.routing.models import ResourceCandidate, ResourceRef

        candidate = ResourceCandidate(
            ResourceRef("primary", "primary", "provider-native-model"),
            driver_id="anthropic-compatible",
            metadata={"quota_resource_id": "account:primary"},
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
        quota = InMemoryQuotaReservations()
        quota.add_resource(
            QuotaResource(
                resource_id="account:primary",
                scope="account",
                metric="requests",
                limit=0,
                window_seconds=60,
                hard_limit=True,
            )
        )

        engine = RouterEngine(snapshot, quota_reservations=quota)

        self.assertEqual([], engine.select_candidates("chat"))

    def test_router_engine_keeps_soft_quota_pressure_eligible_but_scores_it_lower(self):
        from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.routing.models import ResourceCandidate, ResourceRef

        pressured = ResourceCandidate(
            ResourceRef("primary", "primary", "pressured-model"),
            driver_id="anthropic-compatible",
        )
        healthy = ResourceCandidate(
            ResourceRef("secondary", "secondary", "healthy-model"),
            driver_id="anthropic-compatible",
        )
        snapshot = RuntimeConfigSnapshot(
            routes={
                "chat": RouteConfig(
                    route_name="chat",
                    strategy="priority",
                    candidates=[pressured, healthy],
                )
            }
        )
        quota = InMemoryQuotaReservations()
        quota.add_resource(
            QuotaResource(
                resource_id="model:pressured-model",
                scope="model",
                metric="requests",
                limit=0,
                window_seconds=60,
                hard_limit=False,
            )
        )
        quota.add_resource(
            QuotaResource(
                resource_id="model:healthy-model",
                scope="model",
                metric="requests",
                limit=10,
                window_seconds=60,
                hard_limit=True,
            )
        )

        engine = RouterEngine(snapshot, quota_reservations=quota)
        candidates = engine.select_candidates("chat")

        self.assertEqual([healthy.resource_ref, pressured.resource_ref], [candidate.resource_ref for candidate in candidates])
        self.assertEqual({"model:pressured-model": 1}, candidates[1].metadata["quota_soft_pressure_by_resource"])
        self.assertEqual("model:pressured-model", candidates[1].metadata["quota_resource_id"])


if __name__ == '__main__':
    unittest.main()
