"""M7 — Migration smoke test: compile legacy config.yaml → RuntimeConfigSnapshot."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class TestMigrationSmoke(unittest.TestCase):
    def test_compile_file_runs_clean(self):
        """Verify LegacyConfigCompiler().compile_file() runs on HEAD config.yaml without exception."""
        from apps.gateway.config.compiler import LegacyConfigCompiler

        # Locate the repo root from this test file's location
        test_dir = Path(__file__).resolve().parent.parent.parent
        config_path = test_dir / "config.yaml"

        self.assertTrue(config_path.exists(), "config.yaml must exist at project root")

        # Run compilation
        compiler = LegacyConfigCompiler()
        snapshot = compiler.compile_file(config_path)

        # All upstreams become connections
        self.assertIn("proxypal", snapshot.connections)
        self.assertIn("xkiro", snapshot.connections)
        self.assertIn("aibox", snapshot.connections)

        # Main routes are preserved
        route_names = list(snapshot.routes.keys())
        self.assertIn("claude-router-main", route_names)
        self.assertIn("claude-router-fast", route_names)
        self.assertIn("claude-router-engineering", route_names)
        self.assertIn("claude-router-critical", route_names)
        self.assertIn("claude-router-review", route_names)

        # Connections have correct auth metadata
        conn = snapshot.connections["proxypal"]
        self.assertEqual(conn.base_url, "http://127.0.0.1:8317")
        self.assertEqual(conn.auth_mode, "bearer")
        self.assertEqual(conn.token_env, "PROXYPAL_API_KEY")

    def test_compile_dict_matches_compile_file(self):
        """Compile via dict should produce identical structure to file path."""
        import yaml

        from apps.gateway.config.compiler import LegacyConfigCompiler

        test_dir = Path(__file__).resolve().parents[2]
        config_path = test_dir / "config.yaml"
        with open(config_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)

        compiler = LegacyConfigCompiler()
        snap_file = compiler.compile_file(config_path)
        snap_dict = compiler.compile_dict(raw)

        self.assertEqual(set(snap_file.routes.keys()), set(snap_dict.routes.keys()))
        self.assertEqual(set(snap_file.connections.keys()), set(snap_dict.connections.keys()))

        # Same candidates per route
        for name in snap_file.routes:
            file_candidates = [(c.resource_ref.provider_connection_id, c.resource_ref.model_id) for c in snap_file.routes[name].candidates]
            dict_candidates = [(c.resource_ref.provider_connection_id, c.resource_ref.model_id) for c in snap_dict.routes[name].candidates]
            self.assertEqual(file_candidates, dict_candidates, f"Mismatch in route {name}")
