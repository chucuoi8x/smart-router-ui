"""RED tests Step 117 — Models/Resources API Control Plane AC-13."""
import sys
from pathlib import Path
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app

def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")

@pytest.mark.asyncio
async def test_models_requires_auth():
    async with _client() as c:
        r=await c.get("/api/admin/v1/models")
    assert r.status_code==401

@pytest.mark.asyncio
async def test_models_lists_active_route_resources():
    async with _client() as c:
        h={"Authorization":"Bearer test-admin-key"}
        draft=await c.post("/api/admin/v1/revisions",json={"routes":{"model-api-test":{"strategy":"priority","candidates":[{"upstream":"provider-a","model":"model-a","weight":1},{"upstream":"provider-b","model":"model-b"}]}}},headers=h)
        assert draft.status_code==201
        assert (await c.post(f"/api/admin/v1/revisions/{draft.json()['revision_id']}/activate",headers=h)).status_code==200
        r=await c.get("/api/admin/v1/models",headers=h)
    assert r.status_code==200,r.text
    data=r.json()
    assert "items" in data
    items=data["items"]
    assert any(x["route_id"]=="model-api-test" and x["model"]=="model-a" for x in items)
    assert any(x["route_id"]=="model-api-test" and x["model"]=="model-b" for x in items)
    assert "api_key" not in r.text.lower()
    assert "sk-" not in r.text

@pytest.mark.asyncio
async def test_models_supports_route_filter():
    async with _client() as c:
        r=await c.get("/api/admin/v1/models?route_id=no-such-route",headers={"Authorization":"Bearer test-admin-key"})
    assert r.status_code==200
    assert r.json()["items"]==[]
