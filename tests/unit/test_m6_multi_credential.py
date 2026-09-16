"""RED tests Step 121 — Multi-credential per connection AC-03."""
import sys
from pathlib import Path
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app

AUTH = {"Authorization": "Bearer test-admin-key"}

def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")

async def _create_provider(c):
    r = await c.post("/api/admin/v1/providers", json={"template_id":"openai","name":"cred-test-prov","base_url":"https://api.openai.com/v1","api_key":"sk-first"}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()["connection_id"]

@pytest.mark.asyncio
async def test_credentials_requires_auth():
    async with _client() as c:
        r = await c.get("/api/admin/v1/providers/whatever/credentials")
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_add_credential_returns_redacted_and_lists():
    async with _client() as c:
        cid = await _create_provider(c)
        r = await c.post(f"/api/admin/v1/providers/{cid}/credentials", json={"alias":"second-key","api_key":"sk-second-secret"}, headers=AUTH)
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["connection_id"] == cid
        assert data["credential_id"]
        assert data["alias"] == "second-key"
        assert data["credential_present"] is True
        assert "sk-second-secret" not in r.text
        # list should contain at least this credential, independent from primary
        r2 = await c.get(f"/api/admin/v1/providers/{cid}/credentials", headers=AUTH)
        assert r2.status_code == 200, r2.text
        items = r2.json()["items"]
        assert any(x["credential_id"] == data["credential_id"] for x in items)
        assert any(x["alias"] == "second-key" for x in items)
        # no secret in list
        assert "sk-second-secret" not in r2.text

@pytest.mark.asyncio
async def test_add_credential_unknown_provider_404():
    async with _client() as c:
        r = await c.post("/api/admin/v1/providers/nope/credentials", json={"alias":"x","api_key":"sk-x"}, headers=AUTH)
    assert r.status_code == 404

@pytest.mark.asyncio
async def test_list_credentials_unknown_provider_404():
    async with _client() as c:
        r = await c.get("/api/admin/v1/providers/nope/credentials", headers=AUTH)
    assert r.status_code == 404

@pytest.mark.asyncio
async def test_multiple_credentials_independent():
    async with _client() as c:
        cid = await _create_provider(c)
        r1 = await c.post(f"/api/admin/v1/providers/{cid}/credentials", json={"alias":"k1","api_key":"sk-k1"}, headers=AUTH)
        r2 = await c.post(f"/api/admin/v1/providers/{cid}/credentials", json={"alias":"k2","api_key":"sk-k2"}, headers=AUTH)
        assert r1.status_code == 201 and r2.status_code == 201
        assert r1.json()["credential_id"] != r2.json()["credential_id"]
        lst = await c.get(f"/api/admin/v1/providers/{cid}/credentials", headers=AUTH)
        assert lst.json()["total"] >= 2
