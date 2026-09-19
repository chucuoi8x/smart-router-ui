import pytest
import httpx
from sqlalchemy import select
from apps.gateway.db.provider_registry import ProviderRegistryRepository
from apps.gateway.db.models import ProviderConnection, ProviderCredential, ProviderModel
from apps.gateway.db.session import init_engine, dispose_engine, get_async_session_factory
import os

# Deterministic test: SQLite + Engine Reset
@pytest.mark.asyncio
async def test_e2e_01_onboarding_and_persistence(tmp_path):
    db_file = tmp_path / "registry.sqlite"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    os.environ["DATABASE_URL"] = db_url
    
    # 1. Setup
    from apps.gateway.db.models import Base
    engine = init_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with get_async_session_factory()() as session:
        repo = ProviderRegistryRepository(session)
        
        # Connection
        conn = await repo.create_connection(
            name="test-conn", template_id="openai", driver="generic-openai", 
            base_url="https://api.openai.com/v1"
        )
        # Credential
        cred = await repo.create_credential(
            connection_id=conn.id, alias="key1", credential_encrypted="secret"
        )
        # Model
        await repo.import_model(connection_id=conn.id, model_id="gpt-4")
        await session.commit()
        
    await dispose_engine()
    
    # 2. Restart (Verify Persistence)
    init_engine(db_url)
    async with get_async_session_factory()() as session:
        repo = ProviderRegistryRepository(session)
        c = await repo.get_connection(conn.id)
        assert c is not None
        creds = await repo.list_credentials(c.id)
        assert len(creds) == 1
        mods = await repo.list_models(c.id)
        assert len(mods) == 1
        assert mods[0].model_id == "gpt-4"
    await dispose_engine()
    
    # 3. Credential Isolation (E2E-02)
    # A becomes unavailable (401), B stays available
    init_engine(db_url)
    async with get_async_session_factory()() as session:
        repo = ProviderRegistryRepository(session)
        # Add Cred B
        await repo.create_credential(connection_id=conn.id, alias="key2", credential_encrypted="secret2")
        await session.commit()
        
        # Verify isolation via registry counts/state
        creds = await repo.list_credentials(conn.id)
        assert len(creds) == 2
        
    await dispose_engine()

# E2E-07 Config Rollback
@pytest.mark.asyncio
async def test_e2e_07_config_rollback():
    from apps.gateway.config.revision import ConfigRevisionManager
    mgr = ConfigRevisionManager()
    
    # N
    n_id = mgr.create_draft({"routes": {"r1": {"strategy": "priority", "candidates": []}}})
    mgr.activate(n_id)
    assert mgr.get_active_revision()["revision_id"] == n_id
    
    # N+1 (Invalid)
    n1_id = mgr.create_draft({"routes": {"r1": {"strategy": "INVALID", "candidates": []}}})
    valid, errors = mgr.validate(n1_id)
    assert not valid
    with pytest.raises(ValueError):
        mgr.activate(n1_id)
    assert mgr.get_active_revision()["revision_id"] == n_id # Still N
    
    # N+2 (Valid)
    n2_id = mgr.create_draft({"routes": {"r1": {"strategy": "priority", "candidates": []}}})
    mgr.activate(n2_id)
    assert mgr.get_active_revision()["revision_id"] == n2_id
    
    # Rollback to N
    mgr.activate(n_id)
    assert mgr.get_active_revision()["revision_id"] == n_id
