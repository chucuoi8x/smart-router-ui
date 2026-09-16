"""RED tests Step 147 — overview paid-fallback/budget policy summary (AC-10/13)."""
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
async def test_overview_exposes_paid_fallback_policy_and_budget_summary():
    async with _client() as c:
        project = await c.post("/api/admin/v1/projects", json={"name":"overview-budget"}, headers=AUTH)
        assert project.status_code == 201, project.text
        pid = project.json()["project_id"]
        budget = await c.put(f"/api/admin/v1/projects/{pid}/budget", json={"currency":"USD", "ceiling":100, "used":40, "allow_paid_fallback":True}, headers=AUTH)
        assert budget.status_code == 200, budget.text
        policy = await c.put("/api/admin/v1/policies/paid-fallback", json={"enabled":True,"project_id":pid}, headers=AUTH)
        assert policy.status_code == 200, policy.text
        r = await c.get("/api/admin/v1/overview", headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "policies" in data
    assert data["policies"]["paid_fallback"]["enabled"] is True
    assert data["policies"]["paid_fallback"]["project_id"] == pid
    assert "budgets" in data
    assert data["budgets"]["total"] >= 1
    matched = [x for x in data["budgets"]["items"] if x["project_id"] == pid]
    assert matched
    assert matched[0]["remaining"] == 60.0

@pytest.mark.asyncio
async def test_overview_policy_does_not_expose_secret():
    async with _client() as c:
        r = await c.get("/api/admin/v1/overview", headers=AUTH)
    assert r.status_code == 200
    assert "secret_key" not in r.text.lower()
    assert "key_hash" not in r.text.lower()
