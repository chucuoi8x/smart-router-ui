"""RED tests Step 112 — Provider listing cho Control Plane (M6/AC-13)."""
import sys
from pathlib import Path
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app
def _client(): return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")

@pytest.mark.asyncio
async def test_providers_listing_requires_auth():
    async with _client() as c:
        r = await c.get("/api/admin/v1/providers")
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_providers_listing_returns_created_providers_redacted():
    async with _client() as c:
        h = {"Authorization": "Bearer test-admin-key"}
        created = await c.post("/api/admin/v1/providers", json={"template_id":"openai","name":"prov-list-test","base_url":"https://api.openai.com/v1","api_key":"sk-should-not-leak-list"}, headers=h)
        assert created.status_code == 201
        cid = created.json()["connection_id"]
        listed = await c.get("/api/admin/v1/providers", headers=h)
        assert listed.status_code == 200, listed.text
        data = listed.json()
        items = data["items"] if isinstance(data, dict) and "items" in data else data
        assert any(x.get("connection_id")==cid for x in items)
        assert "sk-should-not-leak-list" not in listed.text
        assert "api_key" not in listed.text.lower()
        detail = await c.get(f"/api/admin/v1/providers/{cid}", headers=h)
        assert detail.status_code == 200, detail.text
        assert detail.json()["connection_id"] == cid
        assert "api_key" not in detail.text.lower()

@pytest.mark.asyncio
async def test_providers_detail_404_for_unknown():
    async with _client() as c:
        r = await c.get("/api/admin/v1/providers/does-not-exist", headers={"Authorization":"Bearer test-admin-key"})
    assert r.status_code == 404
