"""RED tests Step 142 — audit export Control Plane (AC-13)."""
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
async def test_audit_export_requires_auth():
    async with _client() as c:
        r = await c.get("/api/admin/v1/audit/export")
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_audit_export_returns_redacted_json():
    async with _client() as c:
        # create an auditable event via revision
        draft = await c.post("/api/admin/v1/revisions", json={"routes": {"exp-test": {"candidates": []}}, "api_key": "sk-export-secret"}, headers=AUTH)
        assert draft.status_code == 201, draft.text
        rid = draft.json()["revision_id"]
        await c.post(f"/api/admin/v1/revisions/{rid}/activate", headers=AUTH)
        r = await c.get("/api/admin/v1/audit/export", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("application/json")
    data = r.json()
    assert "items" in data
    assert "total" in data
    assert "exported_at" in data
    assert isinstance(data["items"], list)
    assert data["total"] >= 1
    assert "sk-export-secret" not in r.text
    assert "api_key" not in r.text.lower()

@pytest.mark.asyncio
async def test_audit_export_supports_action_filter():
    async with _client() as c:
        r = await c.get("/api/admin/v1/audit/export?action=revision.activated", headers=AUTH)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert all(x.get("action") == "revision.activated" for x in items)
    assert "sk-" not in r.text
