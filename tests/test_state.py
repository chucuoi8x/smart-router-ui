import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router import SmartRouter


class FailingCatalogClient:
    async def get(self, *args, **kwargs):
        raise httpx.ConnectError("simulated catalog outage")


class CatalogStateTests(unittest.TestCase):
    def make_router(self, root: Path) -> SmartRouter:
        return SmartRouter(
            {
                "upstreams": {
                    "aibox": {
                        "base_url": "https://example.invalid",
                        "auth": {"mode": "bearer", "token_env": "TEST_AIBOX_KEY"},
                    }
                },
                "routes": {},
                "aibox_catalog_sync": {
                    "docs_url": "https://example.invalid/docs",
                    "pricing_url": "https://example.invalid/pricing",
                    "models_url": "https://example.invalid/v1/models",
                    "state_file": str(root / "catalog.json"),
                    "previous_state_file": str(root / "catalog.previous.json"),
                    "generated_routes_file": str(root / "routes.yaml"),
                    "lock_file": str(root / "sync.lock"),
                },
                "logging": {"level": "CRITICAL"},
            }
        )

    def test_failed_sync_preserves_last_known_good_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            router = self.make_router(root)
            first = {"last_successful_sync": "first", "selected_routes": {}}
            second = {"last_successful_sync": "second", "selected_routes": {}}
            router._persist_catalog(first, {})
            router._persist_catalog(second, {})
            router.catalog = second
            router.clients["aibox"] = FailingCatalogClient()
            previous = os.environ.get("TEST_AIBOX_KEY")
            os.environ["TEST_AIBOX_KEY"] = "local-test-key"
            try:
                result = asyncio.run(router.sync_catalog())
            finally:
                if previous is None:
                    os.environ.pop("TEST_AIBOX_KEY", None)
                else:
                    os.environ["TEST_AIBOX_KEY"] = previous

            self.assertFalse(result["ok"])
            self.assertEqual(json.loads((root / "catalog.json").read_text()), second)
            self.assertEqual(json.loads((root / "catalog.previous.json").read_text()), first)
            self.assertFalse((root / "sync.lock").exists())

    def test_process_does_not_delete_a_lock_it_did_not_acquire(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            router = self.make_router(root)
            lock = root / "sync.lock"
            lock.write_text("another-process", encoding="utf-8")
            previous = os.environ.get("TEST_AIBOX_KEY")
            os.environ["TEST_AIBOX_KEY"] = "local-test-key"
            try:
                result = asyncio.run(router.sync_catalog())
            finally:
                if previous is None:
                    os.environ.pop("TEST_AIBOX_KEY", None)
                else:
                    os.environ["TEST_AIBOX_KEY"] = previous

            self.assertTrue(result["skipped"])
            self.assertTrue(lock.exists())
            self.assertEqual(lock.read_text(encoding="utf-8"), "another-process")


if __name__ == "__main__":
    unittest.main()
