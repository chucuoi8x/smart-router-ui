"""RED tests Step 122 — Import discovered models into route AC-01."""
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
    r = await c.post("/api/admin/v1/providers", json={"template_id":"openai","name":"import-prov","base_url":"https://api.openai.com/v1","api_key":"sk-import"}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()["connection_id"]

@pytest.mark.asyncio
async def test_import_requires_auth():
    async with _client() as c:
        r = await c.post("/api/admin/v1/providers/whatever/models/import", json={"route_id":"r1","models":["m1"]})
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_import_unknown_provider_404():
    async with _client() as c:
        r = await c.post("/api/admin/v1/providers/nope/models/import", json={"route_id":"r1","models":["m1"]}, headers=AUTH)
    assert r.status_code == 404

@pytest.mark.asyncio
async def test_import_adds_candidates_to_route_and_activates():
    async with _client() as c:
        cid = await _create_provider(c)
        # ensure route exists via migration or create empty revision
        # create a base revision with empty route to import into
        rev = await c.post("/api/admin/v1/revisions", json={"routes":{"import-route":{"strategy":"priority","candidates":[]}}, "connections":{}}, headers=AUTH)
        assert rev.status_code == 201
        await c.post(f"/api/admin/v1/revisions/{rev.json()['revision_id']}/activate", headers=AUTH)
        r = await c.post(f"/api/admin/v1/providers/{cid}/models/import", json={"route_id":"import-route","models":["gpt-import-1","gpt-import-2"]}, headers=AUTH)
        assert r.status_code in (200,201), r.text
        data = r.json()
        assert data["route_id"] == "import-route"
        assert data["revision_id"]
        assert "sk-import" not in r.text
        # verify via GET /routes
        lst = await c.get("/api/admin/v1/routes", headers=AUTH)
        assert lst.status_code == 200
        routes = {x["route_id"]: x["config"] for x in lst.json()["items"]}
        assert "import-route" in routes
        cands = routes["import-route"].get("candidates", [])
        models = [x.get("model") for x in cands]
        assert "gpt-import-1" in models
        assert "gpt-import-2" in models
        # upstream matches connection_id
        assert any(x.get("upstream")==cid for x in cands)

@pytest.mark.asyncio
async def test_import_validates_models_required():
    async with _client() as c:
        cid = await _create_provider(c)
        r = await c.post(f"/api/admin/v1/providers/{cid}/models/import", json={"route_id":"import-route","models":[]}, headers=AUTH)
        assert r.status_code == 400
