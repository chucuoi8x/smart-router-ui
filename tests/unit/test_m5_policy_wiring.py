"""Wiring tests cho M5 policy constraint enforcement qua RouterEngine + SmartRouter.

README §31.2 yêu cầu invariant tests: "Router rejects resources that fail
policy constraints" và "paid fallback chỉ dùng khi policy cho phép + budget
pass" (AC-10). Những test này chứng minh bộ lọc được nối đúng vào cả hai
đường routing (RouterEngine và legacy SmartRouter), không chỉ là hàm lẻ.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _snapshot_with_candidates(primary_meta, fallback_meta):
    """RuntimeConfigSnapshot 1 route 'chat', primary + fallback ResourceCandidate."""
    from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
    from apps.gateway.routing.models import ResourceCandidate, ResourceRef

    primary = []
    for i, meta in enumerate(primary_meta):
        primary.append(ResourceCandidate(
            ResourceRef(f"p{i}", f"p{i}", f"primary-{i}"),
            driver_id="anthropic-compatible", metadata=dict(meta)))
    fallback = []
    for i, meta in enumerate(fallback_meta):
        fallback.append(ResourceCandidate(
            ResourceRef(f"f{i}", f"f{i}", f"fallback-{i}"),
            driver_id="anthropic-compatible", metadata=dict(meta)))
    return RuntimeConfigSnapshot(routes={
        "chat": RouteConfig(route_name="chat", strategy="priority",
                            candidates=primary, fallback=fallback)
    })


class RouterEnginePolicyWiringTests(unittest.TestCase):
    def _active_config(self, preset="auto-free"):
        from apps.gateway.routing.scoring import ScoringConfig
        return ScoringConfig(enabled=True, mode="active", preset=preset)

    def test_quality_floor_drops_underperforming_primary(self):
        from apps.gateway.routing.engine import RouterEngine
        snapshot = _snapshot_with_candidates(
            primary_meta=[{"quality_score": 0.5}, {"quality_score": 0.9}],
            fallback_meta=[],
        )
        engine = RouterEngine(snapshot, scoring_config=self._active_config())
        chosen = engine.select_candidates("chat")
        models = [c.resource_ref.model_id for c in chosen]
        self.assertEqual(["primary-1"], models)

    def test_paid_fallback_dropped_when_preset_disallows(self):
        from apps.gateway.routing.engine import RouterEngine
        # 'coding' preset: allow_paid_fallback = False
        snapshot = _snapshot_with_candidates(
            primary_meta=[{}],
            fallback_meta=[{"is_paid": True, "quality_score": 0.9}, {"quality_score": 0.9}],
        )
        engine = RouterEngine(snapshot, scoring_config=self._active_config(preset="coding"))
        chosen = engine.select_candidates("chat")
        models = [c.resource_ref.model_id for c in chosen]
        # paid fallback loại, free fallback giữ, primary giữ
        self.assertIn("primary-0", models)
        self.assertIn("fallback-1", models)
        self.assertNotIn("fallback-0", models)

    def test_disabled_scheduler_keeps_all_candidates(self):
        from apps.gateway.routing.engine import RouterEngine
        from apps.gateway.routing.scoring import ScoringConfig
        snapshot = _snapshot_with_candidates(
            primary_meta=[{"quality_score": 0.1}], fallback_meta=[])
        engine = RouterEngine(snapshot, scoring_config=ScoringConfig())  # disabled
        chosen = engine.select_candidates("chat")
        self.assertEqual(["primary-0"], [c.resource_ref.model_id for c in chosen])


class SmartRouterPolicyWiringTests(unittest.TestCase):
    def _router(self, preset="auto-free"):
        from router import SmartRouter
        config = {
            "routes": {"chat": {"strategy": "priority",
                                "candidates": [{"upstream": "p", "model": "m"}],
                                "fallback": []}},
            "upstreams": {"p": {"base_url": "https://p",
                                "auth": {"mode": "bearer", "token_env": "P_T"}}},
            "smart_scheduler": {"enabled": True, "mode": "active", "preset": preset},
            "logging": {"level": "CRITICAL"},
        }
        return SmartRouter(config)

    def test_helper_drops_low_quality_candidate(self):
        router = self._router()
        from router import Candidate
        low = Candidate(upstream="p", model="low", metadata={"quality_score": 0.2})
        high = Candidate(upstream="p", model="high", metadata={"quality_score": 0.9})
        kept = router._apply_policy_constraints([low, high], "chat", is_fallback=False)
        self.assertEqual(["high"], [c.model for c in kept])

    def test_candidate_order_enforces_quality_from_route_config(self):
        """Metadata quality phải đi từ YAML route đến policy filter thực tế."""
        import asyncio
        from router import SmartRouter

        config = {
            "routes": {"chat": {"strategy": "priority",
                                "candidates": [{"upstream": "p", "model": "low", "quality_score": 0.2}],
                                "fallback": [{"upstream": "p", "model": "high", "quality_score": 0.9}]}},
            "upstreams": {"p": {"base_url": "https://p",
                                "auth": {"mode": "bearer", "token_env": "P_T"}}},
            "smart_scheduler": {"enabled": True, "mode": "active", "preset": "auto-free"},
            "logging": {"level": "CRITICAL"},
        }
        router = SmartRouter(config)

        ordered = asyncio.run(router._candidate_order("chat"))

        self.assertEqual(["high"], [candidate.model for candidate in ordered])

    def test_helper_noop_when_scheduler_inactive(self):
        from router import SmartRouter
        from router import Candidate
        config = {
            "routes": {"chat": {"strategy": "priority",
                                "candidates": [{"upstream": "p", "model": "m"}],
                                "fallback": []}},
            "upstreams": {"p": {"base_url": "https://p",
                                "auth": {"mode": "bearer", "token_env": "P_T"}}},
            "logging": {"level": "CRITICAL"},
        }
        router = SmartRouter(config)  # smart_scheduler absent -> disabled
        low = Candidate(upstream="p", model="low", metadata={"quality_score": 0.0})
        kept = router._apply_policy_constraints([low], "chat", is_fallback=False)
        self.assertEqual(["low"], [c.model for c in kept])


if __name__ == "__main__":
    unittest.main()
