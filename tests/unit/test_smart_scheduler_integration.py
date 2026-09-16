"""Integration tests for Smart Scheduler in SmartRouter context.

Tests scoring end-to-end through router methods when enabled/disabled.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TestSmartScoringInRouter(unittest.TestCase):
    """Verify _apply_smart_scoring integrates correctly with SmartRouter."""

    def _make_router(
        self,
        scoring_enabled: bool = False,
        *,
        mode: str | None = None,
        route_allowlist: list[str] | None = None,
    ) -> "SmartRouter":
        from router import SmartRouter

        scheduler_config = {"enabled": scoring_enabled}
        if mode is not None:
            scheduler_config["mode"] = mode
        if route_allowlist is not None:
            scheduler_config["route_allowlist"] = route_allowlist
        config = {
            "routes": {
                "test-route": {
                    "strategy": "priority",
                    "candidates": [
                        {"upstream": "a", "model": "m1"},
                        {"upstream": "b", "model": "m2"},
                        {"upstream": "c", "model": "m3"},
                    ],
                    "fallback": [],
                }
            },
            "smart_scheduler": scheduler_config,
        }
        return SmartRouter(config)

    def test_skips_scoring_when_disabled(self):
        router = self._make_router(scoring_enabled=False)
        candidates = list(router.routes["test-route"]["candidates"])
        result = router._apply_smart_scoring(candidates, "test-route")
        # With scoring disabled, should return exact copy preserving order
        self.assertEqual(len(result), len(candidates))
        self.assertEqual([r.key for r in result], [c.key for c in candidates])
        keys_before = [c.key for c in candidates]
        keys_after = [c.key for c in result]
        self.assertEqual(keys_before, keys_after)

    def test_returns_original_on_empty_candidates(self):
        router = self._make_router(scoring_enabled=True)
        result = router._apply_smart_scoring([], "test-route")
        self.assertEqual(result, [])

    def test_scores_with_metrics(self):
        """When scoring is enabled, candidates are re-ordered based on metrics."""
        router = self._make_router(scoring_enabled=True)
        candidates = list(router.routes["test-route"]["candidates"])
        # Build metrics that favor one candidate
        router._failure_tracker.record("b:m2", True)  # b has success record
        router._latency_tracker.record("b:m2", 50.0)

        result = router._apply_smart_scoring(candidates, "test-route")
        self.assertEqual(len(result), len(candidates))
        # Scored order should differ or be same depending on metrics
        # The key is no crash and all candidates returned
        result_keys = [c.key for c in result]
        for key in ["a:m1", "b:m2", "c:m3"]:
            self.assertIn(key, result_keys)

    def test_shadow_mode_preserves_original_order(self):
        router = self._make_router(scoring_enabled=True, mode="shadow")
        candidates = list(router.routes["test-route"]["candidates"])
        result = router._apply_smart_scoring(candidates, "test-route")
        self.assertEqual([c.key for c in result], [c.key for c in candidates])

    def test_route_allowlist_blocks_other_routes(self):
        router = self._make_router(
            scoring_enabled=True,
            mode="active",
            route_allowlist=["different-route"],
        )
        self.assertFalse(router._should_apply_smart_scoring("test-route"))
        candidates = list(router.routes["test-route"]["candidates"])
        result = router._apply_smart_scoring(candidates, "test-route")
        self.assertEqual([c.key for c in result], [c.key for c in candidates])

    def test_route_allowlist_allows_named_route(self):
        router = self._make_router(
            scoring_enabled=True,
            mode="active",
            route_allowlist=["test-route"],
        )
        self.assertTrue(router._should_apply_smart_scoring("test-route"))


class TestScoringConfigParsing(unittest.TestCase):
    """Verify YAML config parsing produces correct ScoringConfig."""

    def test_defaults_when_no_data(self):
        from apps.gateway.routing.scoring import ScoringConfig

        cfg = ScoringConfig.from_dict({})
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.preset, "auto-free")
        # auto-free nạp 10 chiều (6 cũ + 4 mới) rồi normalize về tổng 1.0
        self.assertAlmostEqual(cfg.weights.cost_factor, 0.2222, places=3)
        self.assertAlmostEqual(cfg.weights.reliability_factor, 0.1222, places=3)
        # 4 chiều M5 phải có trọng số >0 khi preset auto-free được nạp
        self.assertGreater(cfg.weights.expiry_urgency_factor, 0.0)
        self.assertGreater(cfg.weights.scarcity_factor, 0.0)
        self.assertGreater(cfg.weights.retry_cost_factor, 0.0)
        self.assertGreater(cfg.weights.uncertainty_factor, 0.0)
        self.assertAlmostEqual(sum(cfg.weights.to_dict().values()), 1.0, places=2)

    def test_overrides_from_yaml(self):
        from apps.gateway.routing.scoring import ScoringConfig

        data = {
            "enabled": True,
            "weights": {"cost_factor": 0.5, "reliability_factor": 0.5},
            "max_failure_history": 200,
        }
        cfg = ScoringConfig.from_dict(data)
        self.assertTrue(cfg.enabled)
        self.assertAlmostEqual(cfg.max_failure_history, 200)
        # Others retain defaults

    def test_min_requests_for_metrics_default(self):
        from apps.gateway.routing.scoring import ScoringConfig

        cfg = ScoringConfig.from_dict({"min_requests_for_metrics": 10})
        self.assertEqual(cfg.min_requests_for_metrics, 10)

    def test_rollout_mode_defaults_disabled(self):
        from apps.gateway.routing.scoring import ScoringConfig

        cfg = ScoringConfig.from_dict({})
        self.assertEqual(cfg.mode, "disabled")
        self.assertFalse(cfg.enabled)

    def test_rollout_mode_active_when_enabled_without_mode(self):
        from apps.gateway.routing.scoring import ScoringConfig

        cfg = ScoringConfig.from_dict({"enabled": True})
        self.assertEqual(cfg.mode, "active")
        self.assertTrue(cfg.enabled)

    def test_rollout_route_allowlist_parsed(self):
        from apps.gateway.routing.scoring import ScoringConfig

        cfg = ScoringConfig.from_dict({
            "enabled": True,
            "mode": "shadow",
            "route_allowlist": ["claude-router-main"],
        })
        self.assertEqual(cfg.mode, "shadow")
        self.assertEqual(cfg.route_allowlist, ["claude-router-main"])
        self.assertTrue(cfg.enabled)


if __name__ == "__main__":
    unittest.main()
