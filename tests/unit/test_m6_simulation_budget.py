"""RED tests Step 148 — simulation budget gate AC-10."""
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
async def test_simulation_budget_exhausted_rejects_route():
    """An exhausted project budget rejects simulation before candidate choice."""
    async with _client() as c:
        project = await c.post("/api/admin/v1/projects", json={"name": "sim-budget"}, headers=AUTH)
        assert project.status_code == 201, project.text
        project_id = project.json()["project_id"]
        budget = await c.put(
            f"/api/admin/v1/projects/{project_id}/budget",
            json={"currency": "USD", "ceiling": 50, "used": 50},
            headers=AUTH,
        )
        assert budget.status_code == 200, budget.text
        response = await c.post(
            "/api/admin/v1/routes/simulate",
            json={"route_name": ROUTE, "project_id": project_id},
            headers=AUTH,
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["reason"] == "project_budget_exhausted"
    assert data["selected_resource"] is None
    assert data["candidates"] == []
    assert data["budget"]["project_id"] == project_id
    assert data["budget"]["eligible"] is False
    assert data["budget"]["remaining"] == 0.0


@pytest.mark.asyncio
async def test_simulation_budget_headroom_proceeds_and_reports_gate():
    """A project with budget headroom remains eligible and exposes its gate."""
    async with _client() as c:
        project = await c.post("/api/admin/v1/projects", json={"name": "sim-good"}, headers=AUTH)
        assert project.status_code == 201, project.text
        project_id = project.json()["project_id"]
        budget = await c.put(
            f"/api/admin/v1/projects/{project_id}/budget",
            json={"currency": "USD", "ceiling": 500, "used": 10},
            headers=AUTH,
        )
        assert budget.status_code == 200, budget.text
        response = await c.post(
            "/api/admin/v1/routes/simulate",
            json={"route_name": ROUTE, "project_id": project_id},
            headers=AUTH,
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["reason"] != "project_budget_exhausted"
    assert data["budget"]["project_id"] == project_id
    assert data["budget"]["eligible"] is True
    assert data["budget"]["remaining"] == 490.0


@pytest.mark.asyncio
async def test_simulation_unknown_project_returns_404():
    async with _client() as c:
        response = await c.post(
            "/api/admin/v1/routes/simulate",
            json={"route_name": ROUTE, "project_id": "does-not-exist"},
            headers=AUTH,
        )
    assert response.status_code == 404
