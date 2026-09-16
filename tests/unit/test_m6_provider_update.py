"""RED tests Step 141 — provider update (AC-01)."""
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
async def test_provider_update_requires_auth():
    async with _client() as c:
        r = await c.put("/api/admin/v1/providers/unknown", json={"name": "x"}, headers={})
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_provider_update_unknown_is_404():
    async with _client() as c:
        r = await c.put("/api/admin/v1/providers/does-not-exist", json={"name": "ghost"}, headers=AUTH)
    assert r.status_code == 404

@pytest.mark.asyncio
async def test_provider_update_name_and_base_url():
    async with _client() as c:
        created = await c.post("/api/admin/v1/providers", json={"template_id":"openai","name":"upd-before","base_url":"https://api.openai.com/v1","api_key":"sk-upd-before"}, headers=AUTH)
        assert created.status_code == 201, created.text
        cid = created.json()["connection_id"]
        r = await c.put(f"/api/admin/v1/providers/{cid}", json={"name": "upd-after", "base_url": "https://api.openai.com/v2"}, headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["connection_id"] == cid
    assert data["name"] == "upd-after"
    assert data["base_url"] == "https://api.openai.com/v2"
    assert "sk-upd-before" not in r.text
    assert "api_key" not in r.text.lower()

@pytest.mark.asyncio
async def test_provider_update_api_key_is_redacted():
    async with _client() as c:
        created = await c.post("/api/admin/v1/providers", json={"template_id":"openai","name":"upd-key","base_url":"https://api.openai.com/v1","api_key":"sk-old"}, headers=AUTH)
        assert created.status_code == 201, created.text
        cid = created.json()["connection_id"]
        r = await c.put(f"/api/admin/v1/providers/{cid}", json={"api_key": "sk-new-secret"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert "sk-new-secret" not in r.text
    assert "sk-old" not in r.text
    assert r.json()["credential_present"] is True
