"""P0 final Operations/CI convergence regression tests."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from apps.gateway.security.crypto import require_encryption_key
from apps.gateway.security.ssrf import ProviderURLValidationError, validate_provider_url
from router import app

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"


@pytest.mark.asyncio
async def test_liveness_does_not_probe_failed_dependencies(monkeypatch):
    """Liveness reports process health even when readiness dependencies fail."""

    class DependencyMustNotRun:
        async def connect(self):
            raise AssertionError("liveness must not connect to database")

        async def ping(self):
            raise AssertionError("liveness must not ping Redis")

    monkeypatch.setattr(app.state, "db", DependencyMustNotRun(), raising=False)
    monkeypatch.setattr(app.state, "redis", DependencyMustNotRun(), raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_production_encryption_key_remains_mandatory(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("SMART_ROUTER_ENV", "production")

    with pytest.raises(RuntimeError, match="SMART_ROUTER_ENCRYPTION_KEY is required"):
        require_encryption_key()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/v1",
        "http://[::1]/v1",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "https://user:password@example.com/v1",
    ],
)
def test_ssrf_protection_rejects_internal_and_unsafe_provider_urls(url):
    with pytest.raises(ProviderURLValidationError):
        validate_provider_url(url)


def test_clean_checkout_dev_dependencies_cover_test_runtime():
    requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()

    assert "aiosqlite" in requirements
    assert "fakeredis[lua]" in requirements


def test_ci_has_mandatory_p0_e2e_gate():
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert "p0-e2e" in jobs
    job = jobs["p0-e2e"]
    commands = "\n".join(
        str(step.get("run", "")) for step in job["steps"] if isinstance(step, dict)
    )
    assert "tests/e2e" in commands
    assert "tests/integration/test_e2e_onboarding.py" in commands
    assert "tests/integration/test_e2e_03_multi_dimensional_quota.py" in commands
    assert "tests/integration/test_e2e_06_streaming_memory.py" in commands
    assert "tests/integration/test_streaming_acceptance.py" in commands


def test_ci_static_checks_include_tests_and_dependency_validation():
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    lint_commands = "\n".join(
        str(step.get("run", "")) for step in jobs["lint"]["steps"] if isinstance(step, dict)
    )
    schema_commands = "\n".join(
        str(step.get("run", "")) for step in jobs["schema-check"]["steps"] if isinstance(step, dict)
    )

    assert "ruff check apps router.py tests" in lint_commands
    assert "pip check" in schema_commands
