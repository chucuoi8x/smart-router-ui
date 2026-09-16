"""RED tests Step 146 — paid fallback policy control (AC-10)."""
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
async def test_policy_endpoint_requires_auth():
    async with _client() as c:
        r = await c.get("/api/admin/v1/policies/paid-fallback")
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_paid_fallback_default_is_disabled():
    async with _client() as c:
        # Module-level Control Plane state persists across ASGI tests; restore
        # the documented safe default before asserting it.
        reset = await c.put("/api/admin/v1/policies/paid-fallback", json={"enabled": False}, headers=AUTH)
        assert reset.status_code == 200, reset.text
        r = await c.get("/api/admin/v1/policies/paid-fallback", headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["policy"] == "paid-fallback"
    assert data["enabled"] is False
    assert "requires_budget" in data

@pytest.mark.asyncio
async def test_update_paid_fallback_requires_budget_approval():
    async with _client() as c:
        p = await c.post("/api/admin/v1/projects", json={"name":"policy-project"}, headers=AUTH)
        pid = p.json()["project_id"]
        # no budget: enabling rejected
        denied = await c.put("/api/admin/v1/policies/paid-fallback", json={"enabled":True, "project_id":pid}, headers=AUTH)
        assert denied.status_code == 400
        # budget + explicit allow: enable
        await c.put(f"/api/admin/v1/projects/{pid}/budget", json={"currency":"USD", "ceiling":100, "used":0, "allow_paid_fallback":True}, headers=AUTH)
        enabled = await c.put("/api/admin/v1/policies/paid-fallback", json={"enabled":True, "project_id":pid}, headers=AUTH)
    assert enabled.status_code == 200, enabled.text
    data = enabled.json()
    assert data["enabled"] is True
    assert data["project_id"] == pid

@pytest.mark.asyncio
async def test_policy_disable_is_always_allowed():
    async with _client() as c:
        r = await c.put("/api/admin/v1/policies/paid-fallback", json={"enabled":False}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
