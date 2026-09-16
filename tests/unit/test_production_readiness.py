"""Production readiness regression tests."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_env_example_documents_all_runtime_upstream_credentials():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("PROXYPAL_API_KEY", "XKIRO_API_KEY", "AIBOX_API_KEY"):
        assert f"{name}=" in text


def test_worker_compose_receives_config_and_catalog_settings():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    worker = compose["services"]["worker"]
    env = worker["environment"]
    assert env["SMART_ROUTER_CONFIG"] == "/app/config.yaml"
    assert env["WORKER_POLL_SECONDS"]
    assert "./config.yaml:/app/config.yaml:ro" in worker["volumes"]
    assert "./state:/app/state" in worker["volumes"]


def test_worker_image_contains_runtime_config_and_state_path():
    dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    assert "COPY config.yaml ./" in dockerfile
    assert "COPY state ./state" in dockerfile or "./state:/app/state" in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_runbook_has_current_verification_count_and_no_stale_count():
    text = (ROOT / "docs/ops/runbook.md").read_text(encoding="utf-8")
    assert "519 passed, 6 skipped" in text
    assert "401 passed" not in text


def test_gitignore_excludes_runtime_state_and_python_cache():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "__pycache__/" in text
    assert "state/" in text
    assert ".env" in text
