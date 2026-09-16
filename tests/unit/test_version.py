"""RED tests Step 126 — Version endpoint 1.0."""
import sys
from pathlib import Path
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app

def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")

@pytest.mark.asyncio
async def test_version_endpoint_returns_1_0():
    async with _client() as c:
        r = await c.get("/version")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["version"] == "1.0.0"
    assert data["service"] == "smart-router"

@pytest.mark.asyncio
async def test_version_via_admin_prefix():
    async with _client() as c:
        r = await c.get("/api/admin/v1/version", headers={"Authorization": "Bearer test-admin-key"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "1.0.0"

@pytest.mark.asyncio
async def test_openapi_version_is_1_0():
    async with _client() as c:
        r = await c.get("/openapi.json")
    assert r.status_code == 200
    assert r.json()["info"]["version"] == "1.0.0"
