"""RED tests Step 118 — Settings/Security API Control Plane AC-13."""
import sys
from pathlib import Path
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app

def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")

@pytest.mark.asyncio
async def test_settings_requires_auth():
    async with _client() as c:
        r = await c.get("/api/admin/v1/settings")
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_settings_returns_security_status_redacted():
    async with _client() as c:
        r = await c.get("/api/admin/v1/settings", headers={"Authorization": "Bearer test-admin-key"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "security" in data
    sec = data["security"]
    # encryption key presence reported as boolean, never the value
    assert "encryption_key_configured" in sec
    assert isinstance(sec["encryption_key_configured"], bool)
    # must not leak any actual secret material
    assert "sk-" not in r.text
    assert "test-admin-key" not in r.text

@pytest.mark.asyncio
async def test_settings_update_writes_audit_without_secret():
    async with _client() as c:
        h = {"Authorization": "Bearer test-admin-key"}
        upd = await c.put("/api/admin/v1/settings", json={"log_level": "WARNING"}, headers=h)
        assert upd.status_code == 200, upd.text
        audit = await c.get("/api/admin/v1/audit?action=settings.updated", headers=h)
        assert audit.status_code == 200
        assert any(e.get("key") == "log_level" for e in audit.json()["items"])
