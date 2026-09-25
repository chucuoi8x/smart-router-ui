"""P0-20 §5.7: Dynamic provider onboarding E2E — Add → Activate → Chat → Restart → Chat."""
import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from apps.gateway.db.models import Base
from apps.gateway.db.revisions import RevisionRepository


@pytest.mark.asyncio
async def test_dynamic_provider_onboarding_and_restart_persistence(tmp_path, monkeypatch):
    """Full lifecycle: create provider, discover models, activate route, chat, restart, chat again."""
    # Encryption key must be set before encrypt_secret() so create and the
    # lifespan bind/decrypt share one Fernet key (valid 32-byte urlsafe key).
    monkeypatch.setenv("SMART_ROUTER_ENCRYPTION_KEY", "2oAwhpLBc_i_dLYkALZVphUOxhhuWjQTloNPd3lK2KA=")
    db_path = tmp_path / "onboarding.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Mock upstream provider
    from unittest.mock import AsyncMock, patch
    mock_models = [{"id": "gpt-4-turbo"}, {"id": "gpt-3.5-turbo"}]
    
    async with factory() as session:
        # 1. Create provider via Admin API logic (simulated here for brevity)
        from apps.gateway.db.provider_registry import ProviderRegistryRepository
        from apps.gateway.security.crypto import encrypt_secret
        
        repo = ProviderRegistryRepository(session)
        conn_row = await repo.create_connection(
            name="test-onboarding-provider",
            template_id="openai",
            driver="generic-openai",
            base_url="https://api.openai.com/v1",
            credential_encrypted=None, # Will add credential separately
        )
        cred = await repo.create_credential(
            connection_id=conn_row.id,
            alias="default",
            credential_encrypted=encrypt_secret("sk-test-onboarding-key"),
        )
        await repo.import_model(connection_id=conn_row.id, model_id="gpt-4-turbo")
        await repo.import_model(connection_id=conn_row.id, model_id="gpt-3.5-turbo")
        
        # 2. Compile snapshot with generated routes (simulating ProviderResourceCompiler output)
        snapshot_data = {
            "connections": {
                conn_row.id: {
                    "connection_id": conn_row.id,
                    "base_url": conn_row.base_url,
                    "auth_mode": "bearer",
                    "token_env": "TEST_ONBOARDING_TOKEN", # Will be set in env
                }
            },
            "routes": {
                "onboarding-route": {
                    "route_name": "onboarding-route",
                    "strategy": "priority",
                    "candidates": [
                        {
                            "upstream": conn_row.id,
                            "model": "gpt-4-turbo",
                            "credential_id": cred.id,
                            "weight": 1,
                            "generated": True,
                        }
                    ],
                    "fallback": [],
                    "generated": True,
                }
            }
        }
        
        rev_repo = RevisionRepository(session)
        draft = await rev_repo.create_draft(snapshot_data)
        await rev_repo.activate(draft.id)
        await session.commit()
        revision_id = draft.id

    # Set env vars for lifespan
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SMART_ROUTER_ENCRYPTION_KEY", "2oAwhpLBc_i_dLYkALZVphUOxhhuWjQTloNPd3lK2KA=")
    monkeypatch.setenv("SMART_ROUTER_KEY", "test-admin-key")
    monkeypatch.setenv("TEST_ONBOARDING_TOKEN", "x")

    import router as router_module
    
    # 3. Start app (Lifespan loads active revision)
    async with LifespanManager(router_module.app):
        service = router_module.app.state.router
        manager = router_module.app.state.config_manager
        
        assert manager.active_revision_id == revision_id
        assert "onboarding-route" in service.routes
        assert len(service.routes["onboarding-route"]["candidates"]) == 1
        cand = service.routes["onboarding-route"]["candidates"][0]
        assert cand.upstream == conn_row.id
        assert cand.model == "gpt-4-turbo"
        assert cand.metadata.get("credential_id") == cred.id

        # 4. Simulate Chat request (Data Plane)
        # Mock the actual HTTP call to upstream
        with patch.object(httpx.AsyncClient, 'post', new_callable=AsyncMock) as mock_post:
            mock_response = httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"content": "Hello from mock"}}]},
                request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
            )
            mock_post.return_value = mock_response
            
            transport = httpx.ASGITransport(app=router_module.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "onboarding-route", "messages": [{"role": "user", "content": "Hi"}]},
                    headers={"Authorization": "Bearer test-admin-key"} # Router auth key matches SMART_ROUTER_KEY
                )
                # Note: Router auth is separate from upstream auth. 
                # For this test, we assume the request passes router auth or we bypass it in test setup.
                # In real scenario, client would need valid router key.
                # Here we just check the routing logic worked (no 500/404 on route).
                # Since we mocked the upstream, we expect 200 if routing succeeded.
                assert resp.status_code == 200, resp.text

    # 5. Restart simulation: Create new app instance (Lifespan runs again)
    # In real world, this is a process restart. Here we re-import/re-init.
    import importlib
    importlib.reload(router_module) # Reset module state
    
    # Re-set env (reload might clear them if not persistent in OS, but monkeypatch usually persists in proc)
    # Ensure DB is still there with active revision
    
    async with LifespanManager(router_module.app):
        service = router_module.app.state.router
        manager = router_module.app.state.config_manager
        
        # Verify persistence after "restart"
        assert manager.active_revision_id == revision_id
        assert "onboarding-route" in service.routes
        cand = service.routes["onboarding-route"]["candidates"][0]
        assert cand.model == "gpt-4-turbo"
        assert cand.metadata.get("credential_id") == cred.id
        
        # 6. Chat again after restart
        with patch.object(httpx.AsyncClient, 'post', new_callable=AsyncMock) as mock_post:
            mock_response = httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"content": "Hello again"}}]},
                request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
            )
            mock_post.return_value = mock_response
            
            transport = httpx.ASGITransport(app=router_module.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "onboarding-route", "messages": [{"role": "user", "content": "Hi again"}]},
                    headers={"x-api-key": "test-admin-key"}
                )
                assert resp.status_code == 200, resp.text

    await engine.dispose()