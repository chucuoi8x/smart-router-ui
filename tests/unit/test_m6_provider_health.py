"""RED tests Step 138 — provider health/status Control Plane (AC-01/02/12)."""
import sys
from pathlib import Path
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app

AUTH = {"Authorization": "Bearer test-admin-key"}

def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")

@pytest.mark.asyncio
async def test_provider_health_requires_auth():
    async with _client() as c:
        r = await c.get("/api/admin/v1/providers/unknown/health")
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_provider_health_unknown_is_404():
    async with _client() as c:
        r = await c.get("/api/admin/v1/providers/unknown/health", headers=AUTH)
    assert r.status_code == 404

@pytest.mark.asyncio
async def test_provider_health_returns_redacted_status():
    async with _client() as c:
        created = await c.post("/api/admin/v1/providers", json={"template_id":"openai","name":"health-provider","base_url":"https://api.openai.com/v1","api_key":"sk-health-secret"}, headers=AUTH)
        assert created.status_code in {200, 201}, created.text
        cid = created.json()["connection_id"]
        r = await c.get(f"/api/admin/v1/providers/{cid}/health", headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["connection_id"] == cid
    assert data["status"] in {"unknown", "healthy", "degraded", "unhealthy"}
    assert "checked_at" in data
    assert "sk-health-secret" not in r.text

@pytest.mark.asyncio
async def test_provider_health_summary_is_redacted():
    async with _client() as c:
        r = await c.get("/api/admin/v1/providers/health", headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "items" in data
    assert isinstance(data["items"], list)
    assert "sk-" not in r.text
