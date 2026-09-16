"""RED tests Step 139 — provider health probe execution (AC-01/02/12)."""
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
async def test_health_before_test_is_unknown():
    async with _client() as c:
        created = await c.post("/api/admin/v1/providers", json={"template_id":"openai","name":"probe-before-test","base_url":"https://api.openai.com/v1","api_key":"sk-probe-before"}, headers=AUTH)
        assert created.status_code == 201, created.text
        cid = created.json()["connection_id"]
        r = await c.get(f"/api/admin/v1/providers/{cid}/health", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unknown"
    assert "sk-probe-before" not in r.text

@pytest.mark.asyncio
async def test_health_after_successful_test_is_healthy():
    async with _client() as c:
        created = await c.post("/api/admin/v1/providers", json={"template_id":"openai","name":"probe-after-test","base_url":"https://api.openai.com/v1","api_key":"sk-probe-after"}, headers=AUTH)
        assert created.status_code == 201, created.text
        cid = created.json()["connection_id"]
        tested = await c.post(f"/api/admin/v1/providers/{cid}/test", headers=AUTH)
        assert tested.status_code == 200, tested.text
        r = await c.get(f"/api/admin/v1/providers/{cid}/health", headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "healthy"
    assert "checked_at" in data
    assert data["connection_id"] == cid
    assert "sk-probe-after" not in r.text

@pytest.mark.asyncio
async def test_health_summary_reflects_probe():
    async with _client() as c:
        created = await c.post("/api/admin/v1/providers", json={"template_id":"openai","name":"probe-summary","base_url":"https://api.openai.com/v1","api_key":"sk-probe-summary"}, headers=AUTH)
        assert created.status_code == 201, created.text
        cid = created.json()["connection_id"]
        await c.post(f"/api/admin/v1/providers/{cid}/test", headers=AUTH)
        r = await c.get("/api/admin/v1/providers/health", headers=AUTH)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    matched = [x for x in items if x["connection_id"] == cid]
    assert matched, items
    assert matched[0]["status"] == "healthy"
    assert "sk-probe-summary" not in r.text
