"""RED→GREEN tests for aibox_catalog.sync_catalog (worker-driven catalog sync)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.worker.collectors.aibox_catalog import sync_catalog

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

PRICING_API = {
    "data": [
        {"model_name": "qwen3.6-flash", "quota_type": 0, "model_ratio": 0.0235, "completion_ratio": 2},
        {"model_name": "qwen3.7-flash", "quota_type": 0, "model_ratio": 0.01, "completion_ratio": 4},
        {"model_name": "qwen3.7-plus", "quota_type": 0, "model_ratio": 0.032, "completion_ratio": 4.140625},
        {"model_name": "qwen3.8-max", "quota_type": 0, "model_ratio": 0.2, "completion_ratio": 3},
        {"model_name": "wan2.7-image-pro", "quota_type": 1, "model_price": 0.002},
    ]
}
RUNTIME_MODELS = {
    "data": [
        {"id": "qwen3.6-flash"},
        {"id": "qwen3.7-flash"},
        {"id": "qwen3.7-plus"},
        {"id": "qwen3.8-max"},
        {"id": "wan2.7-image-pro"},
    ]
}


class SyncCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _fetchers(self):
        def fetch_text(url):
            return "public docs text"

        def fetch_json(url, headers=None):
            if url.endswith("/api/pricing"):
                return PRICING_API
            if url.endswith("/v1/models"):
                return RUNTIME_MODELS
            raise AssertionError("unexpected url " + url)

        return fetch_text, fetch_json

    def test_sync_catalog_writes_state_and_generated_routes(self):
        fetch_text, fetch_json = self._fetchers()
        root = Path(self.tmp.name)
        cfg = {
            "docs_url": "https://api.ai-box.vn/docs",
            "pricing_url": "https://api.ai-box.vn/pricing",
            "pricing_api_url": "https://api.ai-box.vn/api/pricing",
            "models_url": "https://api.ai-box.vn/v1/models",
            "quota_per_usd": 500000.0,
            "state_file": str(root / "state/aibox-catalog.json"),
            "previous_state_file": str(root / "state/aibox-catalog.previous.json"),
            "generated_routes_file": str(root / "state/aibox-routes.generated.yaml"),
        }
        count = sync_catalog(
            config=cfg,
            policy=POLICY,
            fetch_text=fetch_text,
            fetch_json=fetch_json,
            token="dummy",
        )
        self.assertGreaterEqual(count, 4)
        state = json.loads((root / "state/aibox-catalog.json").read_text(encoding="utf-8"))
        self.assertIn("qwen3.7-flash", state["selected_routes"]["cheap"])
        self.assertIn("qwen3.7-plus", state["selected_routes"]["engineering"])
        self.assertTrue(state["last_successful_sync"])
        routes_text = (root / "state/aibox-routes.generated.yaml").read_text(encoding="utf-8")
        self.assertIn("claude-router-aibox-cheap", routes_text)
        self.assertIn("qwen3.7-flash", routes_text)

    def test_sync_catalog_missing_token_skips_without_raising(self):
        fetch_text, fetch_json = self._fetchers()
        result = sync_catalog(
            config={"state_file": str(Path(self.tmp.name) / "s.json")},
            policy=POLICY,
            fetch_text=fetch_text,
            fetch_json=fetch_json,
            token=None,
        )
        self.assertEqual(result, 0)
        self.assertFalse(Path(self.tmp.name, "s.json").exists())

    def test_sync_catalog_persists_previous_state_on_rerun(self):
        fetch_text, fetch_json = self._fetchers()
        root = Path(self.tmp.name)
        cfg = {
            "docs_url": "https://api.ai-box.vn/docs",
            "pricing_url": "https://api.ai-box.vn/pricing",
            "pricing_api_url": "https://api.ai-box.vn/api/pricing",
            "models_url": "https://api.ai-box.vn/v1/models",
            "state_file": str(root / "state/aibox-catalog.json"),
            "previous_state_file": str(root / "state/aibox-catalog.previous.json"),
            "generated_routes_file": str(root / "state/aibox-routes.generated.yaml"),
        }
        sync_catalog(config=cfg, policy=POLICY, fetch_text=fetch_text, fetch_json=fetch_json, token="dummy")
        sync_catalog(config=cfg, policy=POLICY, fetch_text=fetch_text, fetch_json=fetch_json, token="dummy")
        self.assertTrue((root / "state/aibox-catalog.previous.json").exists())

    def test_sync_catalog_failure_keeps_last_known_good(self):
        fetch_text, _ = self._fetchers()
        root = Path(self.tmp.name)
        cfg = {
            "docs_url": "https://api.ai-box.vn/docs",
            "pricing_url": "https://api.ai-box.vn/pricing",
            "pricing_api_url": "https://api.ai-box.vn/api/pricing",
            "models_url": "https://api.ai-box.vn/v1/models",
            "state_file": str(root / "state/aibox-catalog.json"),
            "previous_state_file": str(root / "state/aibox-catalog.previous.json"),
            "generated_routes_file": str(root / "state/aibox-routes.generated.yaml"),
        }

        def good_json(url, headers=None):
            if url.endswith("/api/pricing"):
                return PRICING_API
            return RUNTIME_MODELS

        sync_catalog(config=cfg, policy=POLICY, fetch_text=fetch_text, fetch_json=good_json, token="dummy")
        state_before = (root / "state/aibox-catalog.json").read_text(encoding="utf-8")

        def bad_json(url, headers=None):
            raise RuntimeError("network down")

        result = sync_catalog(config=cfg, policy=POLICY, fetch_text=fetch_text, fetch_json=bad_json, token="dummy")
        self.assertEqual(result, 0)
        self.assertEqual((root / "state/aibox-catalog.json").read_text(encoding="utf-8"), state_before)


if __name__ == "__main__":
    unittest.main()
