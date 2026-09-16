"""RED Step 161 — Compose must migrate before serving traffic."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_has_one_shot_migration_service():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    migrate = compose["services"]["migrate"]
    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert migrate["restart"] == "no"
    assert migrate["environment"]["DATABASE_URL"].startswith("${DATABASE_URL")


def test_gateway_and_worker_wait_for_successful_migration():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for service_name in ("gateway", "worker"):
        dependency = compose["services"][service_name]["depends_on"]["migrate"]
        assert dependency["condition"] == "service_completed_successfully"
