"""Stage E integration wiring: SSRF policy, secret redaction, mandatory key, readiness."""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ROOT = Path(__file__).resolve().parents[2]
AUTH = {"Authorization": "Bearer test-admin-key"}
PROBE_SECRET = "sk-probe-secret-abcdef123456"


@pytest.mark.asyncio
async def test_create_provider_rejects_private_base_url_by_default():
    from router import app

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/api/admin/v1/providers",
            json={
                "template_id": "openai",
                "name": "ssrf-blocked",
                "base_url": "http://169.254.169.254/latest/meta-data",
                "api_key": "sk-block-me",
            },
            headers=AUTH,
        )
    assert created.status_code == 400, created.text
    assert "sk-block-me" not in created.text
    detail = str(created.json().get("detail", "")).lower()
    assert "ssrf" in detail or "disallowed" in detail or "not allowed" in detail


@pytest.mark.asyncio
async def test_create_provider_allows_private_base_url_with_explicit_opt_in():
    from router import app

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/api/admin/v1/providers",
            json={
                "template_id": "openai",
                "name": "ssrf-opt-in",
                "base_url": "http://127.0.0.1:11434/v1",
                "api_key": "sk-local-ollama",
                "allow_private_network": True,
            },
            headers=AUTH,
        )
    assert created.status_code == 201, created.text
    assert "sk-local-ollama" not in created.text


@pytest.mark.asyncio
async def test_update_provider_rejects_private_base_url_by_default():
    from router import app

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/api/admin/v1/providers",
            json={
                "template_id": "openai",
                "name": "ssrf-update",
                "base_url": "https://api.example.invalid/v1",
                "api_key": "sk-update-me",
            },
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        cid = created.json()["connection_id"]
        updated = await client.put(
            f"/api/admin/v1/providers/{cid}",
            json={"base_url": "http://localhost:8080/v1"},
            headers=AUTH,
        )
    assert updated.status_code == 400, updated.text


@pytest.mark.asyncio
async def test_provider_test_endpoint_never_returns_plaintext_credential():
    from router import app

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/api/admin/v1/providers",
            json={
                "template_id": "openai",
                "name": "redact-probe",
                "base_url": "https://api.example.invalid/v1",
                "api_key": PROBE_SECRET,
            },
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        cid = created.json()["connection_id"]
        tested = await client.post(f"/api/admin/v1/providers/{cid}/test", headers=AUTH)
        detail = await client.get(f"/api/admin/v1/providers/{cid}", headers=AUTH)
        creds = await client.get(f"/api/admin/v1/providers/{cid}/credentials", headers=AUTH)
        audit = await client.get("/api/admin/v1/audit", headers=AUTH)
    for response in (tested, detail, creds, audit):
        assert PROBE_SECRET not in response.text, response.url
    assert tested.status_code == 200, tested.text


@pytest.mark.asyncio
async def test_ready_endpoint_reports_encryption_and_active_dependency_status(monkeypatch):
    from router import app

    monkeypatch.delenv("SMART_ROUTER_ENCRYPTION_KEY", raising=False)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health/ready")
    body = response.json()
    assert response.status_code in {200, 503}
    assert body["checks"]["encryption"]["status"] == "unavailable"
    assert body["status"] != "ok"
    assert "SMART_ROUTER_ENCRYPTION_KEY" not in response.text


def test_requirements_txt_pins_dev_extras_for_clean_checkout():
    text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "-r requirements.txt" in text
    for package in ("pytest", "pytest-asyncio", "fakeredis", "respx"):
        assert package in text
