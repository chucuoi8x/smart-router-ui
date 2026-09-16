"""RED tests Step 116 — full Docker Compose stack AC-15."""
import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]

class FullStackComposeTests(unittest.TestCase):
    def test_compose_defines_required_services(self):
        data = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        services = data.get("services") or {}
        for svc in ["gateway", "postgres", "redis"]:
            self.assertIn(svc, services, f"missing service: {svc}")
        # worker/web are required for AC-15 but may be profiled — check at least defined
        self.assertIn("worker", services)
        # web may be static nginx placeholder
        self.assertIn("web", services)

    def test_compose_has_healthchecks_and_volumes(self):
        data = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        services = data["services"]
        self.assertIn("healthcheck", services["postgres"])
        self.assertIn("healthcheck", services["redis"])
        self.assertIn("volumes", data)

    def test_compose_gateway_wires_database_and_redis_env(self):
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("DATABASE_URL", text)
        self.assertIn("REDIS_URL", text)

    def test_env_example_documents_required_vars(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        for var in ["DATABASE_URL", "REDIS_URL", "SMART_ROUTER_ENCRYPTION_KEY"]:
            self.assertIn(var, env)

    def test_backup_restore_doc_exists(self):
        self.assertTrue((ROOT / "docs" / "ops" / "backup-restore.md").exists())
