"""PR-05 provider registry persistence contract.

SQLite only exercises repository behavior. PostgreSQL proof runs only when
SMART_ROUTER_TEST_DATABASE_URL points at an isolated PostgreSQL database.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

AUTH = {"Authorization": "Bearer test-admin-key"}


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_provider_credential_and_imported_model_survive_process_restart(registry_db_url):
    """Registry state must live on disk, not in process memory.

    The restart is simulated by disposing the engine and rebuilding it against
    the same database file, which drops every in-process SQLAlchemy identity
    map. If any handler still relied on a module-level dict, the rows would
    vanish here.
    """
    from apps.gateway.db.session import dispose_engine, init_engine
    from router import app

    async with _client(app) as client:
        created = await client.post(
            "/api/admin/v1/providers",
            headers=AUTH,
            json={
                "template_id": "openai",
                "name": "persistent-provider",
                "base_url": "https://api.example.test/v1",
                "api_key": "test-secret-not-returned",
            },
        )
        assert created.status_code == 201, created.text
        connection_id = created.json()["connection_id"]
        credential = await client.post(
            f"/api/admin/v1/providers/{connection_id}/credentials",
            headers=AUTH,
            json={"alias": "secondary", "api_key": "second-secret-not-returned"},
        )
        assert credential.status_code == 201, credential.text

    await dispose_engine()
    init_engine(registry_db_url)

    async with _client(app) as restarted_client:
        provider = await restarted_client.get(f"/api/admin/v1/providers/{connection_id}", headers=AUTH)
        credentials = await restarted_client.get(
            f"/api/admin/v1/providers/{connection_id}/credentials", headers=AUTH
        )

    assert provider.status_code == 200
    assert provider.json()["credential_present"] is True
    assert "test-secret-not-returned" not in provider.text
    assert credentials.status_code == 200
    assert credentials.json()["total"] == 1
    assert "second-secret-not-returned" not in credentials.text


@pytest.mark.asyncio
async def test_admin_module_exposes_no_in_memory_registry():
    """PR-05 acceptance: the old in-memory dictionaries are gone."""
    from apps.gateway.api import admin

    assert not hasattr(admin, "_provider_connections")
    assert not hasattr(admin, "_provider_credentials")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("SMART_ROUTER_TEST_DATABASE_URL", "").startswith("postgresql+asyncpg://"),
    reason="requires isolated PostgreSQL SMART_ROUTER_TEST_DATABASE_URL",
)
async def test_postgresql_provider_registry_survives_engine_restart():
    """Real PostgreSQL integration; never runs against default/live DATABASE_URL."""
    from apps.gateway.db.models import Base, ProviderConnection
    from apps.gateway.db.provider_registry import ProviderRegistryRepository
    from apps.gateway.db.session import dispose_engine, get_async_session_factory, init_engine

    url = os.environ["SMART_ROUTER_TEST_DATABASE_URL"]
    assert "test" in url.lower(), "test database URL must identify isolated test database"
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with get_async_session_factory()() as session:
        repo = ProviderRegistryRepository(session)
        connection = await repo.create_connection(
            name="postgres-restart", template_id="openai", driver="generic-openai",
            base_url="https://api.example.test/v1", credential_encrypted="ciphertext",
        )
        await session.commit()
        connection_id = connection.id
    await dispose_engine()

    init_engine(url)
    async with get_async_session_factory()() as session:
        stored = await session.scalar(select(ProviderConnection).where(ProviderConnection.id == connection_id))
        assert stored is not None
        assert stored.name == "postgres-restart"
    await dispose_engine()
