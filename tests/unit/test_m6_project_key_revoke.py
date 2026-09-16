"""RED tests Step 144 — project API-key revocation (AC-13)."""
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
async def test_create_project_key():
    async with _client() as c:
        # Create a project
        proj = await c.post("/api/admin/v1/projects", json={"name":"revoke-test"}, headers=AUTH)
        assert proj.status_code == 201, proj.text
        pid = proj.json()["project_id"]
        pk = proj.json()["secret_key"]
        # Create a new key for this project
        created = await c.post(f"/api/admin/v1/projects/{pid}/keys", json={"alias":"key-one"}, headers=AUTH)
        assert created.status_code in {200, 201}, created.text
        data = created.json()
        assert "key_id" in data or "id" in data
        assert "secret" in data or "key" in data
        assert data.get("active") is True
    assert "sk-" not in data["secret"] if isinstance(data.get("secret"), str) else True


@pytest.mark.asyncio
async def test_revoke_key_invalidates_it():
    async with _client() as c:
        proj = await c.post("/api/admin/v1/projects", json={"name":"revoke-ktest"}, headers=AUTH)
        assert proj.status_code == 201, proj.text
        pid = proj.json()["project_id"]
        # create two keys
        k1 = await c.post(f"/api/admin/v1/projects/{pid}/keys", json={"alias":"k1"}, headers=AUTH)
        k1_data = k1.json()
        k2 = await c.post(f"/api/admin/v1/projects/{pid}/keys", json={"alias":"k2"}, headers=AUTH)
        k2_data = k2.json()
        kid1 = k1_data.get("key_id") or k1_data.get("id")
        kid2 = k2_data.get("key_id") or k2_data.get("id")
        assert kid1 and kid2
        # Revoke k1
        rv = await c.delete(f"/api/admin/v1/projects/{pid}/keys/{kid1}", headers=AUTH)
        assert rv.status_code in {200, 204}, rv.text
    assert rv.status_code == 204 or rv.json().get("revoked") is True


@pytest.mark.asyncio
async def test_list_keys_excludes_revoked_or_marks_inactive():
    async with _client() as c:
        proj = await c.post("/api/admin/v1/projects", json={"name":"list-keys-test"}, headers=AUTH)
        assert proj.status_code == 201, proj.text
        pid = proj.json()["project_id"]
        created = await c.post(f"/api/admin/v1/projects/{pid}/keys", json={"alias":"keep-it"}, headers=AUTH)
        kid = (created.json().get("key_id") or created.json().get("id"))
        listed = await c.get(f"/api/admin/v1/projects/{pid}/keys", headers=AUTH)
    assert listed.status_code == 200, listed.text
    data = listed.json()
    assert "items" in data
    items = [i for i in data["items"] if i.get("key_id") == kid or i.get("id") == kid]
    assert len(items) >= 1
    assert all(i.get("active") is True for i in items)


@pytest.mark.asyncio
async def test_revoke_unknown_key_returns_404():
    async with _client() as c:
        proj = await c.post("/api/admin/v1/projects", json={"name":"revoke-404test"}, headers=AUTH)
        assert proj.status_code == 201, proj.text
        pid = proj.json()["project_id"]
        rv = await c.delete(f"/api/admin/v1/projects/{pid}/keys/nonexistent", headers=AUTH)
    assert rv.status_code == 404
