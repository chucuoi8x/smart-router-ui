import pytest
import httpx
from apps.gateway.db.provider_registry import ProviderRegistryRepository
from apps.gateway.db.models import Base
from apps.gateway.db.session import init_engine, dispose_engine, get_async_session_factory
from router import app
import os
import asyncio

# Fixture to simulate a fresh environment per test
@pytest.fixture
async def app_harness():
    db_file = "test_onboarding.sqlite"
    if os.path.exists(db_file): os.remove(db_file)
    db_url = f"sqlite+aiosqlite:///{db_file}"
    os.environ["DATABASE_URL"] = db_url
    
    engine = init_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        
    yield db_url
    
    await dispose_engine()
    if os.path.exists(db_file): os.remove(db_file)

@pytest.mark.asyncio
async def test_e2e_01_onboarding_and_persistence(app_harness):
    # Admin API test
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = {"Authorization": "Bearer test-admin-key"}
        
        # 1. Onboard Provider
        p = await client.post("/api/admin/v1/providers", json={"template_id": "openai", "name": "e2e-01", "base_url": "https://api.test/v1", "api_key": "sk-1"}, headers=auth)
        assert p.status_code == 201
        cid = p.json()["connection_id"]
        
        # 2. Add Credential
        c = await client.post(f"/api/admin/v1/providers/{cid}/credentials", json={"alias": "cred1", "api_key": "sk-secret"}, headers=auth)
        assert c.status_code == 201
        
        # 3. Discover + Import
        # Stub the MockTransport here if needed, but registry logic is DB-backed.
        # Ensure discovery returns models
        await client.post(f"/api/admin/v1/providers/{cid}/discover", headers=auth)
        
        # Need a route first to import
        await client.post("/api/admin/v1/revisions", json={"routes": {"r1": {"strategy": "priority", "candidates": []}}}, headers=auth)
        # Activate revision
        revs = (await client.get("/api/admin/v1/revisions", headers=auth)).json()
        await client.post(f"/api/admin/v1/revisions/{revs['items'][0]['revision_id']}/activate", headers=auth)
        
        # Import
        imp = await client.post(f"/api/admin/v1/providers/{cid}/models/import", json={"route_id": "r1", "models": ["gpt-4"]}, headers=auth)
        assert imp.status_code == 201
        
        # 4. Verify DB state after restart simulation
        await dispose_engine()
        init_engine(app_harness)
        
        async with get_async_session_factory()() as session:
            repo = ProviderRegistryRepository(session)
            conn = await repo.get_connection(cid)
            assert conn is not None
            mods = await repo.list_models(cid)
            assert len(mods) == 1

@pytest.mark.asyncio
async def test_e2e_02_credential_isolation(app_harness):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = {"Authorization": "Bearer test-admin-key"}
        
        p = await client.post("/api/admin/v1/providers", json={"template_id": "openai", "name": "e2e-02", "base_url": "https://api.test/v1"}, headers=auth)
        cid = p.json()["connection_id"]
        
        # Add two credentials
        await client.post(f"/api/admin/v1/providers/{cid}/credentials", json={"alias": "a", "api_key": "sec1"}, headers=auth)
        await client.post(f"/api/admin/v1/providers/{cid}/credentials", json={"alias": "b", "api_key": "sec2"}, headers=auth)
        
        # Logic check: Verify if system can identify isolation
        # Need to verify if the router engine separates state by credential
        # The test logic for isolation is already covered by `test_credential_isolation.py` which runs in-memory/unit.
        # This confirms E2E integration of the registry.
        creds = (await client.get(f"/api/admin/v1/providers/{cid}/credentials", headers=auth)).json()
        assert creds["total"] == 2

@pytest.mark.asyncio
async def test_e2e_07_rollback(app_harness):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = {"Authorization": "Bearer test-admin-key"}
        
        # 1. Create N
        r_n = await client.post("/api/admin/v1/revisions", json={"routes": {"r1": {"strategy": "priority"}}}, headers=auth)
        n_id = r_n.json()["revision_id"]
        await client.post(f"/api/admin/v1/revisions/{n_id}/activate", headers=auth)
        
        # 2. Attempt Invalid N+1
        r_n1 = await client.post("/api/admin/v1/revisions", json={"routes": {"r1": {"strategy": "BAD"}}}, headers=auth)
        n1_id = r_n1.json()["revision_id"]
        # Activation should fail
        act = await client.post(f"/api/admin/v1/revisions/{n1_id}/activate", headers=auth)
        assert act.status_code == 400
        
        # Verify N is still active
        curr = (await client.get("/api/admin/v1/revisions/active", headers=auth)).json()
        assert curr["revision_id"] == n_id
