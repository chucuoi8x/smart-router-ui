"""Acceptance smoke coverage for implemented 1.0 Control Plane slices."""
import sys
from pathlib import Path
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app

AUTH={"Authorization":"Bearer test-admin-key"}

def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")

@pytest.mark.asyncio
async def test_ac13_overview_routes_models_settings_are_auth_guarded():
    async with client() as c:
        for path in ["/overview","/routes","/models","/settings","/providers","/revisions","/audit"]:
            r=await c.get("/api/admin/v1"+path)
            assert r.status_code==401, (path,r.status_code,r.text)

@pytest.mark.asyncio
async def test_ac14_provider_response_and_audit_never_contain_key():
    secret="sk-acceptance-secret"
    async with client() as c:
        r=await c.post("/api/admin/v1/providers",json={"template_id":"openai","name":"ac14","base_url":"https://example.invalid/v1","api_key":secret},headers=AUTH)
        assert r.status_code==201
        for path in ["/providers","/audit","/overview"]:
            x=await c.get("/api/admin/v1"+path,headers=AUTH)
            assert secret not in x.text
            assert "credential_encrypted" not in x.text

@pytest.mark.asyncio
async def test_ac01_create_test_discover_import_and_route():
    async with client() as c:
        created=await c.post("/api/admin/v1/providers",json={"template_id":"openai","name":"ac01","base_url":"https://example.invalid/v1","api_key":"sk-ac01"},headers=AUTH)
        assert created.status_code==201
        cid=created.json()["connection_id"]
        # Built-in stub driver validates without network and discover returns empty safely.
        assert (await c.post(f"/api/admin/v1/providers/{cid}/test",headers=AUTH)).status_code==200
        assert (await c.post(f"/api/admin/v1/providers/{cid}/discover",headers=AUTH)).status_code==200
        draft=await c.post("/api/admin/v1/revisions",json={"routes":{"ac01-route":{"strategy":"priority","candidates":[]}}},headers=AUTH)
        assert draft.status_code==201
        assert (await c.post(f"/api/admin/v1/revisions/{draft.json()['revision_id']}/activate",headers=AUTH)).status_code==200
        imported=await c.post(f"/api/admin/v1/providers/{cid}/models/import",json={"route_id":"ac01-route","models":["model-ac01"]},headers=AUTH)
        assert imported.status_code==201
        routes=(await c.get("/api/admin/v1/routes",headers=AUTH)).json()
        assert any(x["route_id"]=="ac01-route" for x in routes["items"])

@pytest.mark.asyncio
async def test_ac16_migration_audit_and_active_revision():
    async with client() as c:
        payload={"yaml_data":"upstreams: {}\nroutes:\n  smoke:\n    strategy: priority\n    candidates: []"}
        r=await c.post("/api/admin/v1/migration/yaml",json=payload,headers=AUTH)
        assert r.status_code==201
        assert r.json()["activated"] is True
        audit=await c.get("/api/admin/v1/audit?action=migration.yaml",headers=AUTH)
        assert audit.status_code==200
        assert audit.json()["total"]>=1
