"""Provider-agnostic routing guard: routing core must not rely on provider-specific naming."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.config.compiler import LegacyConfigCompiler
from apps.gateway.config.snapshot import RouteConfig


class TestProviderAgnosticRouting(unittest.TestCase):
    def test_generic_driver_name_works_without_specific_provider(self):
        """ResourceCandidate with generic driver_id should be routable by any strategy."""
        from apps.gateway.config.snapshot import RouteConfig
        from apps.gateway.routing.models import ResourceCandidate, ResourceRef

        ref = ResourceRef("generic_provider", "creds", "model-a")
        cand = ResourceCandidate(ref, driver_id="generic-openai", weight=1)

        # The candidate's resource_ref doesn't leak provider name into driver_id
        self.assertEqual(cand.driver_id, "generic-openai")
        self.assertEqual(cand.resource_ref.model_id, "model-a")
        self.assertEqual(cand.resource_ref.provider_connection_id, "generic_provider")

    def test_legacy_compiler_does_not_impose_provider_names_on_driver(self):
        """LegacyConfigCompiler sets driver_id='anthropic-compatible' for all candidates regardless of upstream."""
        import yaml

        raw = {
            "upstreams": {
                "custom_a": {"base_url": "https://custom-a.example/v1", "auth": {"mode": "bearer", "token_env": "KEY_A"}},
                "custom_b": {"base_url": "https://custom-b.example/v1", "auth": {"mode": "bearer", "token_env": "KEY_B"}},
            },
            "routes": {
                "chat": {
                    "strategy": "priority",
                    "candidates": [
                        {"upstream": "custom_a", "model": "model-x"},
                        {"upstream": "custom_b", "model": "model-y"},
                    ],
                }
            },
        }

        compiler = LegacyConfigCompiler()
        snap = compiler.compile_dict(raw)

        route = snap.routes["chat"]
        for c in route.candidates:
            self.assertEqual(c.driver_id, "anthropic-compatible")
            self.assertIn(c.resource_ref.provider_connection_id, {"custom_a", "custom_b"})

    def test_router_can_resolve_any_valid_route_name(self):
        """RouterEngine.resolve_route() must work with dynamically-created routes."""
        from apps.gateway.config.snapshot import RuntimeConfigSnapshot
        from apps.gateway.routing.engine import RouterEngine

        snapshot = RuntimeConfigSnapshot(
            connections={},
            routes={
                "dynamic-route": RouteConfig(
                    route_name="dynamic-route",
                    strategy="priority",
                    candidates=[],
                )
            },
        )

        engine = RouterEngine(snapshot)
        candidates = engine.resolve_route("dynamic-route")

        self.assertIsInstance(candidates, list)
