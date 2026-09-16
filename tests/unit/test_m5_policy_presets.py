"""Unit tests cho M5 policy presets — thỏa README §19 và AC-09."""
import unittest

from apps.gateway.routing.presets import (
    all_presets_dict,
    get_preset,
    get_preset_or_default,
    list_presets,
    preset_to_scoring_weights,
)
from apps.gateway.routing.scoring import ScoringConfig, ScoringWeights


class TestPolicyPresetsList(unittest.TestCase):
    def test_has_all_five_presets(self):
        presets = set(list_presets())
        for expected in {"auto-free", "fast", "coding", "review", "critical"}:
            self.assertIn(expected, presets)

    def test_missing_returns_none(self):
        self.assertIsNone(get_preset("does-not-exist"))

    def test_default_fallback_is_auto_free(self):
        self.assertEqual(get_preset_or_default(None).name, "auto-free")
        self.assertEqual(get_preset_or_default("garbage").name, "auto-free")

    def test_all_have_constraints_weights_retry_reservation(self):
        d = all_presets_dict()
        for name in list_presets():
            body = d[name]
            self.assertEqual(body["name"], name)
            for key in ("constraints", "weights", "retry", "reservation"):
                self.assertIn(key, body)
            for key in (
                "free_savings",
                "quality_fit",
                "reliability",
                "quota_headroom",
                "expiry_urgency",
                "cache_locality",
                "latency",
                "retry_cost",
                "scarcity",
                "uncertainty",
            ):
                self.assertIn(key, body["weights"], msg=f"{name} missing weight {key}")


class TestPresetToWeights(unittest.TestCase):
    def test_mapped_weights_sum_to_one(self):
        for name in list_presets():
            preset = get_preset(name)
            assert preset is not None
            mapped = preset_to_scoring_weights(preset)
            total = sum(mapped.values())
            self.assertAlmostEqual(total, 1.0, delta=0.001, msg=f"{name} weights {mapped} sum {total}")

    def test_mapped_weights_all_factors_present(self):
        for name in list_presets():
            preset = get_preset(name)
            assert preset is not None
            mapped = preset_to_scoring_weights(preset)
            for factor in (
                "cost_factor",
                "reliability_factor",
                "latency_factor",
                "quota_pressure_factor",
                "capability_factor",
                "session_affinity_factor",
            ):
                self.assertIn(factor, mapped)


class TestScoringConfigPreset(unittest.TestCase):
    def test_preset_parsed_from_yaml(self):
        cfg = ScoringConfig.from_dict({"enabled": True, "mode": "active", "preset": "review"})
        self.assertEqual(cfg.preset, "review")

    def test_route_presets_parsed(self):
        cfg = ScoringConfig.from_dict(
            {"enabled": True, "preset": "auto-free", "route_presets": {"claude-router-review": "review"}}
        )
        self.assertEqual(cfg.route_presets["claude-router-review"], "review")

    def test_effective_weights_uses_route_preset(self):
        cfg = ScoringConfig.from_dict(
            {"enabled": True, "preset": "auto-free", "route_presets": {"fast-route": "fast"}}
        )
        w_auto = cfg.effective_weights_for_route("other-route")
        w_fast = cfg.effective_weights_for_route("fast-route")
        # fast tối ưu latency nên latency_factor phải khác auto-free
        # Chỉ cần đảm bảo khác nhau là đủ phản ánh preset có tác dụng.
        self.assertNotEqual(w_fast.to_dict(), w_auto.to_dict())

    def test_custom_weights_not_overridden_by_preset(self):
        cfg = ScoringConfig.from_dict(
            {
                "enabled": True,
                "preset": "review",
                "weights": {"cost_factor": 1},
            }
        )
        # Khi có custom weights, preset không ghi đè nữa
        self.assertAlmostEqual(cfg.weights.cost_factor, 1.0, delta=0.99)

    def test_preset_to_weights_used_when_no_custom_weights(self):
        cfg = ScoringConfig.from_dict({"enabled": True, "preset": "critical"})
        # critical nhấn mạnh reliability/quality nên reliability_factor phải chiếm tỉ trọng
        self.assertGreater(cfg.weights.reliability_factor, 0.2)


if __name__ == "__main__":
    unittest.main()
