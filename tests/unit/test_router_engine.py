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


if __name__ == '__main__':
    unittest.main()
