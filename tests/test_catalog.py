import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aibox_catalog import build_records, select_routes


POLICY = {
    "cheap": {
        "allowed_families": ["deepseek", "qwen"],
        "preferred_markers": ["flash", "lite"],
        "max_input_price_per_million": 0.05,
        "max_output_price_per_million": 0.10,
        "max_candidates": 3,
    },
    "engineering": {
        "allowed_families": ["glm", "deepseek", "qwen", "kimi"],
        "preferred_markers": ["pro", "max", "code"],
        "max_input_price_per_million": 0.20,
        "max_output_price_per_million": 0.60,
        "max_candidates": 3,
    },
    "critical_review": {"manual_preference": ["qwen3.8-max"]},
    "global_deny_patterns": ["*image*", "*embedding*", "*audio*", "*tts*", "*rerank*"],
}


class CatalogPolicyTests(unittest.TestCase):
    def test_cheap_verified_model_is_eligible(self):
        records, public = build_records(
            ["deepseek-v5-flash"],
            "Public model deepseek-v5-flash input $0.04 output $0.08",
            "deepseek-v5-flash: input $0.04 per million; output $0.08 per million",
            POLICY,
        )
        self.assertIn("deepseek-v5-flash", public)
        self.assertTrue(records[0].cheap_eligible)
        self.assertEqual(select_routes(records, POLICY)["cheap"], ["deepseek-v5-flash"])

    def test_new_qwen_critical_candidate_is_not_auto_promoted(self):
        records, _ = build_records(
            ["qwen4-max"],
            "Public model qwen4-max input 0.20 output 0.60",
            "qwen4-max: input 0.20; output 0.60",
            POLICY,
        )
        self.assertEqual(records[0].classification, "critical_candidate")
        self.assertFalse(records[0].engineering_eligible)
        self.assertEqual(select_routes(records, POLICY)["engineering"], [])

    def test_unknown_price_never_promotes(self):
        records, _ = build_records(
            ["deepseek-v6-flash"],
            "Public model deepseek-v6-flash",
            "deepseek-v6-flash pricing pending",
            POLICY,
        )
        self.assertFalse(records[0].cheap_eligible)
        self.assertEqual(records[0].reason, "price unknown")

    def test_unknown_family_does_not_route(self):
        records, _ = build_records(
            ["super-model-x"],
            "Public model super-model-x input $0.01 output $0.02",
            "super-model-x: input $0.01; output $0.02",
            POLICY,
        )
        self.assertEqual(records[0].classification, "unknown")
        self.assertEqual(select_routes(records, POLICY)["cheap"], [])

    def test_public_confirmation_does_not_use_arbitrary_substrings(self):
        records, _ = build_records(
            ["qwen3.7"],
            "Public model qwen3.7-max input $0.10 output $0.20",
            "qwen3.7-max: input $0.10; output $0.20",
            POLICY,
        )
        self.assertFalse(records[0].public)
        self.assertEqual(records[0].reason, "runtime-only; no public confirmation")

    def test_bracketed_context_variant_accepts_public_base_name(self):
        records, _ = build_records(
            ["deepseek-v5-flash[1m]"],
            "Public model deepseek-v5-flash",
            "deepseek-v5-flash: input $0.04; output $0.08",
            POLICY,
        )
        self.assertTrue(records[0].public)
        self.assertFalse(records[0].cheap_eligible)

    def test_price_without_currency_marker_stays_unknown(self):
        records, _ = build_records(
            ["deepseek-v7-flash"],
            "Public model deepseek-v7-flash",
            "deepseek-v7-flash input 0.01 output 0.02",
            POLICY,
        )
        self.assertFalse(records[0].cheap_eligible)
        self.assertEqual(records[0].reason, "price unknown")

    def test_rerank_model_is_denied(self):
        # rerank models are not chat LLMs; they must never be auto-promoted
        records, _ = build_records(
            ["qwen3-rerank"],
            "Public model qwen3-rerank input $0.01 output $0.01",
            "qwen3-rerank: input $0.01; output $0.01",
            POLICY,
        )
        record = records[0]
        self.assertTrue(record.denied)
        self.assertFalse(record.engineering_eligible)
        self.assertEqual(select_routes(records, POLICY)["engineering"], [])

    def test_newapi_pricing_confirms_public_and_prices(self):
        entries = [
            {"model_name": "deepseek-v4-flash", "quota_type": 0, "model_ratio": 0.0175,
             "completion_ratio": 2.142857142857},
            {"model_name": "glm-5.2", "quota_type": 0, "model_ratio": 0.0725, "completion_ratio": 2},
            {"model_name": "wan2.7-image-pro", "quota_type": 1, "model_price": 0.002},
        ]
        records, public = build_records(
            ["deepseek-v4-flash", "glm-5.2", "wan2.7-image-pro"],
            "",
            "",
            POLICY,
            newapi_entries=entries,
        )
        by_name = {r.name: r for r in records}
        self.assertIn("deepseek-v4-flash", public)
        self.assertTrue(by_name["deepseek-v4-flash"].public)
        self.assertTrue(by_name["deepseek-v4-flash"].cheap_eligible)
        self.assertAlmostEqual(by_name["deepseek-v4-flash"].prices["input_per_million"], 0.035)
        self.assertAlmostEqual(by_name["deepseek-v4-flash"].prices["output_per_million"], 0.075)
        self.assertTrue(by_name["glm-5.2"].engineering_eligible)
        # per-call (quota_type=1) entries have no per-token price and are not promoted
        self.assertIn("wan2.7-image-pro", public)


if __name__ == "__main__":
    unittest.main()
