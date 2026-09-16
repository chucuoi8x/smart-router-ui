"""RED tests Step 145 — budget ceiling Control Plane (AC-10)."""
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
async def test_budget_endpoint_requires_auth():
    async with _client() as c:
        r = await c.get("/api/admin/v1/budgets")
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_create_and_list_budget_ceiling():
    async with _client() as c:
        p = await c.post("/api/admin/v1/projects", json={"name":"budget-project"}, headers=AUTH)
        assert p.status_code == 201, p.text
        pid = p.json()["project_id"]
        created = await c.put(f"/api/admin/v1/projects/{pid}/budget", json={"currency":"USD", "ceiling":100.0, "used":25.0, "allow_paid_fallback":False}, headers=AUTH)
        assert created.status_code == 200, created.text
        data = created.json()
        assert data["project_id"] == pid
        assert data["ceiling"] == 100.0
        assert data["remaining"] == 75.0
        assert data["allow_paid_fallback"] is False
        listed = await c.get("/api/admin/v1/budgets", headers=AUTH)
    assert listed.status_code == 200, listed.text
    assert any(x["project_id"] == pid for x in listed.json()["items"])

@pytest.mark.asyncio
async def test_budget_rejects_negative_or_used_over_ceiling():
    async with _client() as c:
        p = await c.post("/api/admin/v1/projects", json={"name":"budget-invalid"}, headers=AUTH)
        pid = p.json()["project_id"]
        r = await c.put(f"/api/admin/v1/projects/{pid}/budget", json={"ceiling":-1}, headers=AUTH)
        assert r.status_code == 400
        r2 = await c.put(f"/api/admin/v1/projects/{pid}/budget", json={"ceiling":10, "used":11}, headers=AUTH)
    assert r2.status_code == 400

@pytest.mark.asyncio
async def test_budget_unknown_project_404():
    async with _client() as c:
        r = await c.put("/api/admin/v1/projects/nope/budget", json={"ceiling":1}, headers=AUTH)
    assert r.status_code == 404
