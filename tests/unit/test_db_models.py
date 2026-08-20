import importlib.util
import unittest
from pathlib import Path


class DBModelsBaselineTests(unittest.TestCase):
    def test_models_metadata_has_expected_tables(self):
        from apps.gateway.db.models import Base, ProviderConnection, ConfigRevision, UsageLedger

        tables = Base.metadata.tables
        self.assertIn("provider_connections", tables)
        self.assertIn("config_revisions", tables)
        self.assertIn("request_ledger", tables)
        self.assertIn("attempt_ledger", tables)
        self.assertIn("usage_ledger", tables)
        self.assertIn("quota_resources", tables)

    def test_quota_resource_state_model_instantiation(self):
        from apps.gateway.db.models import QuotaResourceState

        resource = QuotaResourceState(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            scope="tenant-a",
            metric="total_token",
            limit=100,
            used=25,
            window_seconds=60,
            safety_buffer=10,
            hard_limit=True,
            source="provider_api",
            confidence="exact",
            shared_group_id="account-weekly-123",
        )

        self.assertEqual(resource.resource_id, "tokens:tenant-a:gpt-4o:minute")
        self.assertEqual(resource.limit, 100)
        self.assertEqual(resource.used, 25)
        self.assertEqual(resource.safety_buffer, 10)
        self.assertTrue(resource.hard_limit)
        self.assertEqual(resource.source, "provider_api")
        self.assertEqual(resource.confidence, "exact")
        self.assertEqual(resource.shared_group_id, "account-weekly-123")

    def test_provider_connection_model_instantiation(self):
        from apps.gateway.db.models import ProviderConnection

        conn = ProviderConnection(
            id="conn_123",
            name="OpenAI Main",
            template_id="openai",
            driver="generic_openai",
            base_url="https://api.openai.com/v1",
            credential_encrypted="enc_secret",
            is_active=True,
        )
        self.assertEqual(conn.id, "conn_123")
        self.assertEqual(conn.name, "OpenAI Main")
        self.assertTrue(conn.is_active)

    def test_config_revision_model_instantiation(self):
        from apps.gateway.db.models import ConfigRevision

        rev = ConfigRevision(
            id="rev_abc123",
            snapshot_data={"providers": []},
            is_active=True,
        )
        self.assertEqual(rev.id, "rev_abc123")
        self.assertEqual(rev.snapshot_data, {"providers": []})
        self.assertTrue(rev.is_active)

    def test_usage_ledger_model_instantiation(self):
        from apps.gateway.db.models import UsageLedger

        ledger = UsageLedger(
            id="led_123",
            request_id="req_xyz",
            provider_id="conn_123",
            model="gpt-4o",
            prompt_tokens=100,
            completion_tokens=50,
            estimated_cost=0.0015,
        )
        self.assertEqual(ledger.request_id, "req_xyz")
        self.assertEqual(ledger.prompt_tokens, 100)
        self.assertEqual(ledger.completion_tokens, 50)

    def test_initial_migration_revision_is_importable(self):
        migration_path = Path("migrations/versions/001_initial_baseline.py")
        self.assertTrue(migration_path.exists())

        spec = importlib.util.spec_from_file_location("initial_baseline", migration_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)

        self.assertEqual(module.revision, "001_initial_baseline")
        self.assertIsNone(module.down_revision)
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))

    def test_usage_ledger_provenance_migration_is_importable(self):
        migration_path = Path("migrations/versions/002_usage_ledger_provenance.py")
        self.assertTrue(migration_path.exists())

        spec = importlib.util.spec_from_file_location("usage_ledger_provenance", migration_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)

        self.assertEqual(module.revision, "002_usage_ledger_provenance")
        self.assertEqual(module.down_revision, "001_initial_baseline")
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))

    def test_request_attempt_ledger_migration_is_importable(self):
        migration_path = Path("migrations/versions/003_request_attempt_ledger.py")
        self.assertTrue(migration_path.exists())

        spec = importlib.util.spec_from_file_location("request_attempt_ledger", migration_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)

        self.assertEqual(module.revision, "003_request_attempt_ledger")
        self.assertEqual(module.down_revision, "002_usage_ledger_provenance")
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))

    def test_quota_resource_migration_is_importable(self):
        migration_path = Path("migrations/versions/004_quota_resources.py")
        self.assertTrue(migration_path.exists())

        spec = importlib.util.spec_from_file_location("quota_resources", migration_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)

        self.assertEqual(module.revision, "004_quota_resources")
        self.assertEqual(module.down_revision, "003_request_attempt_ledger")
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))


if __name__ == "__main__":
    unittest.main()
