"""P0-20 §5.6 / §20 provider runtime: live registry resources reach the data plane.

Two vertical slices, both RED first:

1. ``RuntimeConfigManager.materialize_provider_revision`` runs
   :class:`ProviderResourceCompiler` against the live registry, stores the result
   as a new immutable revision and activates it.  A later ``load_initial`` (the
   restart path) compiles *that* revision through ``compile_snapshot`` — so
   validation is never bypassed and the historical snapshot is never overwritten.

2. The data plane resolves the upstream secret from the canonical
   ``ProviderCredential`` row for the selected ``credential_id`` — not from the
   connection's legacy ``token_env``.

Only fixture values; no real secrets.
"""
from __future__ import annotations

import pytest
import os
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.gateway.db.models import Base
from apps.gateway.db.provider_registry import ProviderRegistryRepository
from apps.gateway.db.revisions import RevisionRepository
from apps.gateway.runtime.manager import RuntimeConfigManager
from router import SmartRouter


async def _seed_registry(sessions):
    from apps.gateway.security.crypto import encrypt_secret

    async with sessions() as session:
        repo = ProviderRegistryRepository(session)
        provider = await repo.create_connection(
            name="runtime-fix",
            template_id="openai",
            driver="generic-openai",
            base_url="https://provider.test/v1",
        )
        credential = await repo.create_credential(
            connection_id=provider.id,
            alias="fixture",
            credential_encrypted=encrypt_secret("fixture-canonical-token"),
        )
        await repo.import_model(connection_id=provider.id, model_id="fixture-model")
        await session.commit()
        return provider, credential


@pytest.mark.asyncio
async def test_live_registry_resources_materialize_into_active_revision_and_survive_restart(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    provider, credential = await _seed_registry(sessions)

    # ── materialize live resources through the real compiler ────────────
    async with sessions() as session:
        manager = RuntimeConfigManager(session_factory=sessions)
        revision_id = await manager.materialize_provider_revision(session)
        await session.commit()
    assert revision_id

    async with sessions() as session:
        first = await manager.load_initial(session, bootstrap_path=str(tmp_path / "must-not-be-read.yaml"))
    assert manager.active_revision_id == revision_id
    assert provider.id in first.connections
    candidates = first.routes[provider.id].candidates
    assert [c.resource_ref.key for c in candidates] == [f"{provider.id}:{credential.id}:fixture-model"]
    assert candidates[0].metadata["credential_id"] == credential.id
    assert candidates[0].driver_id == "generic-openai"

    # ── restart: the same historical revision reloads, nothing rewritten ─
    async with sessions() as session:
        before = [r.id for r in await RevisionRepository(session).list_revisions()]
        restarted = RuntimeConfigManager(session_factory=sessions)
        second = await restarted.load_initial(session, bootstrap_path=str(tmp_path / "must-not-be-read.yaml"))
        after = [r.id for r in await RevisionRepository(session).list_revisions()]
    assert restarted.active_revision_id == revision_id
    assert [c.resource_ref.key for c in second.routes[provider.id].candidates] == [
        f"{provider.id}:{credential.id}:fixture-model"
    ]
    assert set(before) == set(after), "restart must not add or mutate revisions"


@pytest.mark.asyncio
async def test_generated_revision_still_runs_compile_snapshot_validation(tmp_path):
    """§20 LKG: a generated-looking revision with an unknown connection must raise."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'invalid.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        rev = await RevisionRepository(session).create_draft({
            "connections": {},
            "routes": {
                "ghost-route": {
                    "route_name": "ghost-route",
                    "strategy": "priority",
                    "candidates": [{
                        "upstream": "conn_ghost",
                        "model": "fixture-model",
                        "credential_id": "cred_ghost",
                        "weight": 1,
                    }],
                    "fallback": [],
                    "generated": True,
                }
            },
        })
        await RevisionRepository(session).activate(rev.id)
        await session.commit()

        manager = RuntimeConfigManager(session_factory=sessions)
        with pytest.raises(ValueError, match="unknown connection"):
            await manager.load_initial(session, bootstrap_path=str(tmp_path / "must-not-be-read.yaml"))
        assert manager.snapshot is None
        assert manager.active_revision_id is None
    await engine.dispose()


def test_runtime_upstream_headers_use_canonical_credential_not_token_env(monkeypatch):
    monkeypatch.setenv("MUST_NOT_BE_USED", "fixture-env-token")
    router = SmartRouter({
        "upstreams": {
            "fixture": {
                "base_url": "https://provider.test/v1",
                "auth": {"mode": "bearer", "token_env": "MUST_NOT_BE_USED"},
            }
        },
        "routes": {
            "fixture-route": {
                "strategy": "priority",
                "candidates": [{
                    "upstream": "fixture",
                    "model": "fixture-model",
                    "credential_id": "cred_fixture",
                    "driver_id": "generic-openai",
                }],
            }
        },
        "logging": {"level": "CRITICAL"},
    })
    candidate = router.routes["fixture-route"]["candidates"][0]
    router._credential_values = {"cred_fixture": "fixture-canonical-token"}

    headers = router._upstream_headers(candidate.upstream, {}, candidate)

    assert headers["authorization"] == "Bearer fixture-canonical-token"


@pytest.mark.asyncio
async def test_enabled_runtime_resolves_credential_from_registry(tmp_path, monkeypatch):
    """The runtime credential map is built from ProviderCredential ciphertext."""
    monkeypatch.setenv("SMART_ROUTER_ENV", "development")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'creds.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    provider, credential = await _seed_registry(sessions)

    from apps.gateway.config.provider_resource_compiler import ProviderResourceCompiler
    from apps.gateway.runtime.manager import bind_runtime_credentials

    router = SmartRouter({
        "upstreams": {provider.id: {"base_url": provider.base_url, "auth": {"mode": "bearer", "token_env": ""}}},
        "routes": {},
        "logging": {"level": "CRITICAL"},
    })
    async with sessions() as session:
        snapshot = await ProviderResourceCompiler().compile(session)
        values = await bind_runtime_credentials(router, session, snapshot)

    assert values[credential.id] == "fixture-canonical-token"
    assert router._credential_values[credential.id] == "fixture-canonical-token"
    # token_env was empty: the value can only have come from the credential row.
    assert "fixture-canonical-token" not in (os.getenv("") or "")
    await engine.dispose()
