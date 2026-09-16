"""RED tests Step 149 — simulation paid-fallback policy awareness AC-10."""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app

AUTH = {"Authorization": "Bearer test-admin-key"}
ROUTE = "claude-router-main"


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_simulation_exposes_paid_fallback_policy_disabled():
    """Simulation response includes paid_fallback policy when disabled."""
    async with _client() as c:
        # Ensure default is disabled
        await c.put("/api/admin/v1/policies/paid-fallback", json={"enabled": False}, headers=AUTH)
        resp = await c.post(
            "/api/admin/v1/routes/simulate",
            json={"route_name": ROUTE},
            headers=AUTH,
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    policy = data.get("policy")
    assert policy is not None, "response must include 'policy' section"
    assert isinstance(policy, dict), "'policy' must be a dict"
    assert policy.get("paid_fallback_enabled") is False
    assert policy.get("requires_budget") is True


@pytest.mark.asyncio
async def test_simulation_exposes_paid_fallback_policy_enabled():
    """Simulation shows paid fallback enabled when policy allows it."""
    async with _client() as c:
        project = await c.post("/api/admin/v1/projects", json={"name": "sim-policy"}, headers=AUTH)
        pid = project.json()["project_id"]
        await c.put(
            f"/api/admin/v1/projects/{pid}/budget",
            json={"currency": "USD", "ceiling": 500, "used": 0, "allow_paid_fallback": True},
            headers=AUTH,
        )
        enable = await c.put(
            "/api/admin/v1/policies/paid-fallback",
            json={"enabled": True, "project_id": pid},
            headers=AUTH,
        )
        assert enable.status_code == 200, enable.text

        resp = await c.post(
            "/api/admin/v1/routes/simulate",
            json={"route_name": ROUTE, "project_id": pid},
            headers=AUTH,
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    policy = data.get("policy")
    assert policy is not None
    assert policy.get("paid_fallback_enabled") is True
    assert policy.get("project_id") == pid
