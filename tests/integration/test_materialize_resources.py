"""§20 provider resource materialization integration proof.

Real SQLite registry rows feed compiler and RouterEngine.  No driver/compiler
execution is mocked.  Credentials use test-only ciphertext and no secrets.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.gateway.db.models import Base
from apps.gateway.db.provider_registry import ProviderRegistryRepository
from apps.gateway.routing.engine import RouterEngine


@pytest.mark.asyncio
async def test_enabled_connection_credentials_and_models_materialize_into_independent_router_resources():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        registry = ProviderRegistryRepository(session)
        connection = await registry.create_connection(
            name="fake-openai",
            template_id="openai",
            driver="generic-openai",
            base_url="https://upstream.test/v1",
        )
        first = await registry.create_credential(
            connection_id=connection.id,
            alias="first",
            credential_encrypted="test-ciphertext-first",
        )
        second = await registry.create_credential(
            connection_id=connection.id,
            alias="second",
            credential_encrypted="test-ciphertext-second",
        )
        await registry.create_credential(
            connection_id=connection.id,
            alias="disabled",
            credential_encrypted="test-ciphertext-disabled",
            enabled=False,
        )
        await registry.import_model(connection_id=connection.id, model_id="model-a")
        await registry.import_model(connection_id=connection.id, model_id="model-b")
        await registry.import_model(connection_id=connection.id, model_id="disabled-model", enabled=False)
        await session.commit()

        from apps.gateway.config.provider_resource_compiler import ProviderResourceCompiler

        snapshot = await ProviderResourceCompiler().compile(session)

    candidates = RouterEngine(snapshot).select_candidates(connection.id)
    assert {candidate.resource_ref.key for candidate in candidates} == {
        f"{connection.id}:{first.id}:model-a",
        f"{connection.id}:{first.id}:model-b",
        f"{connection.id}:{second.id}:model-a",
        f"{connection.id}:{second.id}:model-b",
    }
    assert {candidate.driver_id for candidate in candidates} == {"generic-openai"}
    assert {candidate.metadata["credential_id"] for candidate in candidates} == {first.id, second.id}

    await engine.dispose()
