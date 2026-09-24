import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.quota.runtime_index import RuntimeQuotaIndex
from apps.gateway.routing.engine import RouterEngine


def _engine_with_local_quota(snapshot, quota):
    index = RuntimeQuotaIndex()
    index.replace_all(quota.list_resources())
    return RouterEngine(snapshot, quota_reservations=quota, quota_index=index)


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

    def test_legacy_compiler_preserves_quota_metadata_from_yaml_candidates(self):
        from tempfile import TemporaryDirectory

        from apps.gateway.config.compiler import LegacyConfigCompiler

        yaml_text = """
upstreams:
  primary:
    base_url: https://primary.example
    auth:
      mode: bearer
      token_env: PRIMARY_TOKEN
  backup:
    base_url: https://backup.example
    auth:
      mode: bearer
      token_env: BACKUP_TOKEN
routes:
  chat:
    strategy: priority
    generated: true
    candidates:
      - upstream: primary
        model: fast-model
        weight: 7
        quota_resource_id: account:primary
        quota_resource_ids:
          - account:primary
          - model:fast-model
    fallback:
      - upstream: backup
        model: fallback-model
        weight: 9
        quota_resource_id: null
        quota_resource_ids: malformed-scalar
"""
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(yaml_text, encoding="utf-8")
            snapshot = LegacyConfigCompiler().compile_file(config_path)

        route = snapshot.routes["chat"]
        self.assertTrue(route.generated)
        primary = route.candidates[0]
        fallback = route.fallback[0]

        self.assertEqual("primary", primary.resource_ref.provider_connection_id)
        self.assertEqual("fast-model", primary.resource_ref.model_id)
        self.assertEqual("anthropic-compatible", primary.driver_id)
        self.assertEqual(7, primary.weight)
        self.assertEqual("account:primary", primary.metadata["quota_resource_id"])
        self.assertEqual(["account:primary", "model:fast-model"], primary.metadata["quota_resource_ids"])

        self.assertEqual("backup", fallback.resource_ref.provider_connection_id)
        self.assertEqual("fallback-model", fallback.resource_ref.model_id)
        self.assertEqual("anthropic-compatible", fallback.driver_id)
        self.assertEqual(1, fallback.weight)
        self.assertIsNone(fallback.metadata["quota_resource_id"])
        self.assertEqual("malformed-scalar", fallback.metadata["quota_resource_ids"])

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

        engine = _engine_with_local_quota(snapshot, quota)
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

        engine = _engine_with_local_quota(snapshot, quota)

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

        engine = _engine_with_local_quota(snapshot, quota)
        candidates = engine.select_candidates("chat")

        self.assertEqual([healthy.resource_ref, pressured.resource_ref], [candidate.resource_ref for candidate in candidates])
        self.assertEqual({"model:pressured-model": 1}, candidates[1].metadata["quota_soft_pressure_by_resource"])
        self.assertEqual("model:pressured-model", candidates[1].metadata["quota_resource_id"])

    def test_router_engine_filters_candidate_when_any_metadata_quota_constraint_fails(self):
        from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.routing.models import ResourceCandidate, ResourceRef

        blocked = ResourceCandidate(
            ResourceRef("primary", "primary", "multi-model"),
            driver_id="anthropic-compatible",
            metadata={"quota_resource_ids": ["account:primary", "model:multi-model"]},
        )
        fallback = ResourceCandidate(
            ResourceRef("secondary", "secondary", "fallback-model"),
            driver_id="anthropic-compatible",
        )
        snapshot = RuntimeConfigSnapshot(
            routes={
                "chat": RouteConfig(
                    route_name="chat",
                    strategy="priority",
                    candidates=[blocked, fallback],
                )
            }
        )
        quota = InMemoryQuotaReservations()
        quota.add_resource(QuotaResource("account:primary", "account", "requests", 10, 60))
        quota.add_resource(QuotaResource("model:multi-model", "model", "requests", 0, 60))
        quota.add_resource(QuotaResource("model:fallback-model", "model", "requests", 10, 60))

        engine = _engine_with_local_quota(snapshot, quota)
        candidates = engine.select_candidates("chat")

        self.assertEqual([fallback.resource_ref], [candidate.resource_ref for candidate in candidates])

    def test_router_engine_missing_metadata_quota_id_does_not_hide_known_failure(self):
        from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.routing.models import ResourceCandidate, ResourceRef

        candidate = ResourceCandidate(
            ResourceRef("primary", "primary", "multi-model"),
            driver_id="anthropic-compatible",
            metadata={"quota_resource_ids": ["missing:quota", "model:multi-model"]},
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
        quota.add_resource(QuotaResource("model:multi-model", "model", "requests", 0, 60))

        engine = _engine_with_local_quota(snapshot, quota)

        self.assertEqual([], engine.select_candidates("chat"))

    def test_router_engine_deduplicates_shared_quota_group_constraints(self):
        from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.routing.models import ResourceCandidate, ResourceRef

        candidate = ResourceCandidate(
            ResourceRef("primary", "primary", "multi-model"),
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
        quota = InMemoryQuotaReservations()
        quota.add_resource(QuotaResource("model:a", "model", "requests", 1, 60, shared_group_id="account:shared"))
        quota.add_resource(QuotaResource("model:b", "model", "requests", 1, 60, shared_group_id="account:shared"))

        engine = _engine_with_local_quota(snapshot, quota)
        candidates = engine.select_candidates("chat")

        self.assertEqual([candidate.resource_ref], [selected.resource_ref for selected in candidates])
        self.assertEqual(["model:a"], candidates[0].metadata["quota_resource_ids"])

    def test_router_engine_multi_quota_ids_take_precedence_over_scalar_metadata(self):
        from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.routing.models import ResourceCandidate, ResourceRef

        candidate = ResourceCandidate(
            ResourceRef("primary", "primary", "multi-model"),
            driver_id="anthropic-compatible",
            metadata={
                "quota_resource_id": "scalar:ignored",
                "quota_resource_ids": ["account:primary", "model:multi-model"],
            },
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
        quota.add_resource(QuotaResource("scalar:ignored", "account", "requests", 0, 60))
        quota.add_resource(QuotaResource("account:primary", "account", "requests", 10, 60))
        quota.add_resource(QuotaResource("model:multi-model", "model", "requests", 10, 60))

        engine = _engine_with_local_quota(snapshot, quota)
        candidates = engine.select_candidates("chat")

        self.assertEqual([candidate.resource_ref], [selected.resource_ref for selected in candidates])
        self.assertEqual(["account:primary", "model:multi-model"], candidates[0].metadata["quota_resource_ids"])

    def test_router_engine_all_missing_metadata_quota_ids_remain_unconstrained(self):
        from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
        from apps.gateway.quota.reservations import InMemoryQuotaReservations
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.routing.models import ResourceCandidate, ResourceRef

        candidate = ResourceCandidate(
            ResourceRef("primary", "primary", "multi-model"),
            driver_id="anthropic-compatible",
            metadata={"quota_resource_ids": ["missing:a", "missing:b"]},
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

        engine = RouterEngine(snapshot, quota_reservations=InMemoryQuotaReservations())

        self.assertEqual([candidate.resource_ref], [selected.resource_ref for selected in engine.select_candidates("chat")])

    def test_router_engine_ranks_near_limit_candidate_after_healthier_peer(self):
        from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.routing.models import ResourceCandidate, ResourceRef

        nearly_exhausted = ResourceCandidate(
            ResourceRef("primary", "primary", "near-limit"),
            driver_id="anthropic-compatible",
        )
        healthy = ResourceCandidate(
            ResourceRef("secondary", "secondary", "healthy"),
            driver_id="anthropic-compatible",
        )
        snapshot = RuntimeConfigSnapshot(
            routes={
                "chat": RouteConfig(
                    route_name="chat",
                    strategy="priority",
                    candidates=[nearly_exhausted, healthy],
                )
            }
        )
        quota = InMemoryQuotaReservations()
        quota.add_resource(QuotaResource("model:near-limit", "model", "requests", 10, 60, used=9))
        quota.add_resource(QuotaResource("model:healthy", "model", "requests", 10, 60, used=1))

        engine = _engine_with_local_quota(snapshot, quota)
        candidates = engine.select_candidates("chat")

        self.assertEqual([healthy.resource_ref, nearly_exhausted.resource_ref], [candidate.resource_ref for candidate in candidates])

    def test_smart_router_engine_mode_uses_async_quota_filter_without_nested_loop_error(self):
        import asyncio
        import os
        from unittest.mock import patch

        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource
        from router import SmartRouter

        config = {
            "routes": {
                "chat": {
                    "strategy": "priority",
                    "candidates": [{"upstream": "primary", "model": "blocked"}],
                    "fallback": [{"upstream": "backup", "model": "fallback"}],
                }
            },
            "upstreams": {
                "primary": {"base_url": "https://primary", "auth": {"mode": "bearer", "token_env": "PRIMARY_TOKEN"}},
                "backup": {"base_url": "https://backup", "auth": {"mode": "bearer", "token_env": "BACKUP_TOKEN"}},
            },
            "logging": {"level": "CRITICAL"},
        }
        quota = InMemoryQuotaReservations()
        quota.add_resource(QuotaResource("model:blocked", "model", "requests", 0, 60))
        quota.add_resource(QuotaResource("model:fallback", "model", "requests", 10, 60))

        router = SmartRouter(config, quota_reservations=quota)
        candidates = asyncio.run(router._candidate_order("chat"))

        self.assertEqual(["backup:backup:fallback"], [candidate.key for candidate in candidates])

    def test_smart_router_always_uses_router_engine(self):
        """PR-04: RouterEngine là engine duy nhất — không còn flag USE_ROUTER_ENGINE."""
        import asyncio
        from unittest.mock import MagicMock
        from router import SmartRouter

        config = {
            'routes': {
                'test-route': {
                    'strategy': 'priority',
                    'candidates': [
                        {'upstream': 'primary', 'model': 'model-a', 'weight': 5},
                        {'upstream': 'secondary', 'model': 'model-b', 'weight': 3}
                    ]
                }
            },
            'upstreams': {},
            'logging': {'level': 'CRITICAL'}
        }
        router = SmartRouter(config)
        
        # Assert: router_engine luôn được khởi tạo (không phụ thuộc env var)
        self.assertIsNotNone(router.router_engine)
        
        # Mock engine's select_candidates_async để trả về candidate mong muốn
        mock_candidate = MagicMock()
        mock_candidate.resource_ref.provider_connection_id = 'primary'
        mock_candidate.resource_ref.model_id = 'model-a'
        mock_candidate.weight = 5
        mock_candidate.metadata = {}
        router.router_engine.select_candidates_async = unittest.mock.AsyncMock(return_value=[mock_candidate])
        
        # Act: call _candidate_order
        candidates = asyncio.run(router._candidate_order('test-route'))
        
        # Engine luôn được dùng bất chấp env vars
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].upstream, 'primary')
        self.assertEqual(candidates[0].model, 'model-a')

    def test_smart_router_record_failure_trips_router_engine_circuit(self):
        import asyncio
        import os
        from router import Candidate, SmartRouter
        from unittest.mock import patch

        config = {
            'routes': {
                'test-route': {
                    'strategy': 'priority',
                    'candidates': [
                        {'upstream': 'primary', 'model': 'model-a'},
                        {'upstream': 'secondary', 'model': 'model-b'},
                    ],
                }
            },
            'upstreams': {
                'primary': {'base_url': 'https://primary', 'auth': {'mode': 'bearer', 'token_env': 'PRIMARY_TOKEN'}},
                'secondary': {'base_url': 'https://secondary', 'auth': {'mode': 'bearer', 'token_env': 'SECONDARY_TOKEN'}},
            },
            'logging': {'level': 'CRITICAL'},
        }
        router = SmartRouter(config)
        classification = {
            'kind': 'RATE_LIMIT',
            'scope': 'credential/model/connection',
            'retry_after': '60',
            'reset_at': None,
            'consumption_uncertainty': 'unknown',
        }

        asyncio.run(router._record_failure(Candidate('primary', 'model-a'), 429, 'rate limited', classification))
        candidates = router.router_engine.select_candidates('test-route')

        self.assertEqual(['secondary'], [c.resource_ref.provider_connection_id for c in candidates])

if __name__ == '__main__':
    unittest.main()
