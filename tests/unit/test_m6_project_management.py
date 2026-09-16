"""RED tests Step 128 — Project + API-key management AC-13 (M6)."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from router import app

AUTH = {"Authorization": "Bearer test-admin-key"}


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_projects_requires_auth():
    async with _client() as c:
        r = await c.get("/api/admin/v1/projects")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_project_returns_redacted_key():
    secret = "sk-project-secret-key"
    async with _client() as c:
        r = await c.post(
            "/api/admin/v1/projects",
            json={"name": "my-project", "description": "test project"},
            headers=AUTH,
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert "project_id" in data
        assert data["name"] == "my-project"
        assert data["secret_key"]  # auto-generated
        assert secret not in r.text
        created_key = data["secret_key"]
        # list should show it exists
        lst = await c.get("/api/admin/v1/projects", headers=AUTH)
        assert lst.status_code == 200
        items = lst.json()["items"]
        assert any(x["project_id"] == data["project_id"] for x in items)
        # key never appears in plaintext
        assert secret not in lst.text
        assert "sk-project-secret-key" not in lst.text
        assert created_key != "sk-project-secret-key"


@pytest.mark.asyncio
async def test_get_project_unknown_404():
    async with _client() as c:
        r = await c.get("/api/admin/v1/projects/nope", headers=AUTH)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_projects_multiple():
    async with _client() as c:
        r1 = await c.post("/api/admin/v1/projects", json={"name": "proj-1"}, headers=AUTH)
        r2 = await c.post("/api/admin/v1/projects", json={"name": "proj-2"}, headers=AUTH)
        assert r1.status_code == 201 and r2.status_code == 201
        lst = await c.get("/api/admin/v1/projects", headers=AUTH)
        assert lst.status_code == 200
        ids = {x["project_id"] for x in lst.json()["items"]}
        assert len(ids) >= 2


@pytest.mark.asyncio
async def test_delete_project_audits_and_removes_keys():
    async with _client() as c:
        r = await c.post("/api/admin/v1/projects", json={"name": "to-delete"}, headers=AUTH)
        assert r.status_code == 201
        pid = r.json()["project_id"]
        del_r = await c.delete(f"/api/admin/v1/projects/{pid}", headers=AUTH)
        assert del_r.status_code == 200
        assert del_r.json()["deleted"] is True
        lst = await c.get("/api/admin/v1/projects", headers=AUTH)
        assert all(x["project_id"] != pid for x in lst.json()["items"])
        audit = await c.get("/api/admin/v1/audit?action=project.deleted", headers=AUTH)
        assert audit.status_code == 200
        assert any(e.get("project_id") == pid for e in audit.json()["items"])
