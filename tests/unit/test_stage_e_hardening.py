"""Stage E P0-17 through P0-20 hardening contracts."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_production_requires_explicit_fernet_key(monkeypatch):
    from apps.gateway.security.crypto import require_encryption_key

    monkeypatch.setenv("SMART_ROUTER_ENV", "production")
    monkeypatch.delenv("SMART_ROUTER_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SMART_ROUTER_ENCRYPTION_KEY"):
        require_encryption_key()


def test_development_fallback_requires_explicit_development_mode(monkeypatch):
    from apps.gateway.security.crypto import encrypt_secret

    monkeypatch.delenv("SMART_ROUTER_ENV", raising=False)
    monkeypatch.delenv("SMART_ROUTER_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="explicit development mode"):
        encrypt_secret("secret")

    monkeypatch.setenv("SMART_ROUTER_ENV", "development")
    assert encrypt_secret("secret") != "secret"


def test_secret_redaction_removes_nested_values_and_keeps_safe_fields():
    from apps.gateway.security.redaction import redact_secrets

    result = redact_secrets({"api_key": "leak", "name": "provider", "nested": {"token": "leak", "status": "ok"}})
    assert result == {"name": "provider", "nested": {"status": "ok"}}
    assert "leak" not in str(result)


def test_provider_url_blocks_private_targets_by_default():
    from apps.gateway.security.ssrf import ProviderURLValidationError, validate_provider_url

    for url in ("http://127.0.0.1:8080", "http://169.254.169.254/latest", "http://[::1]/v1", "file:///etc/passwd", "http://localhost:11434"):
        with pytest.raises(ProviderURLValidationError):
            validate_provider_url(url)


def test_provider_url_allows_private_target_only_with_explicit_opt_in():
    from apps.gateway.security.ssrf import validate_provider_url

    assert validate_provider_url("http://127.0.0.1:8080", allow_private_network=True) == "http://127.0.0.1:8080"


def test_readiness_checks_real_dependencies_and_runtime_state(monkeypatch):
    from router import _readiness_payload

    class Connection:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class Database:
        def __init__(self):
            self.connection = Connection()

        async def connect(self):
            return self.connection

    class Redis:
        async def ping(self):
            return True

    class Router:
        clients = {"provider": object()}
        config = {"routes": {"default": {}}}

    monkeypatch.setenv("SMART_ROUTER_ENCRYPTION_KEY", "configured")
    database = Database()
    payload = asyncio.run(_readiness_payload(router=Router(), database=database, redis_client=Redis()))
    assert payload["status"] == "ok"
    assert database.connection.closed is True
    assert payload["checks"]["database"]["status"] == "ok"
    assert payload["checks"]["redis"]["status"] == "ok"
    assert payload["checks"]["runtime_snapshot"]["status"] == "ok"
    assert payload["checks"]["encryption"]["status"] == "ok"


def test_readiness_is_not_ok_when_required_dependency_is_unavailable(monkeypatch):
    from router import _readiness_payload

    class BrokenDatabase:
        async def connect(self):
            raise OSError("unavailable")

    monkeypatch.setenv("SMART_ROUTER_ENCRYPTION_KEY", "configured")
    payload = asyncio.run(_readiness_payload(router=None, database=BrokenDatabase(), redis_client=None))
    assert payload["status"] == "unavailable"
    assert payload["checks"]["database"]["status"] == "unavailable"


def test_declared_dev_dependencies_and_clean_checkout_ci():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = ROOT / ".github" / "workflows" / "ci.yml"
    assert workflow.is_file()
    for package in ("pytest", "pytest-asyncio", "fakeredis", "respx"):
        assert f'"{package}' in project
    text = workflow.read_text(encoding="utf-8")
    for gate in ("lint", "unit", "integration", "migration", "docker", "acceptance"):
        assert gate in text
