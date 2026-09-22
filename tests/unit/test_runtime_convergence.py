"""Runtime authority convergence tests — real SQLite, no mock compiler/engine.

P0 §4: RuntimeConfigManager must load active DB revision on startup,
bootstrap from YAML only when no active exists, activate/rollback atomically,
and survive restart.  These tests use a real SQLite DB and the real
LegacyConfigCompiler / RuntimeConfigSnapshot — only the upstream HTTP
boundary is mocked (no real provider calls).
"""
from __future__ import annotations

import asyncio
import json
import os
import textwrap
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apps.gateway.config.snapshot import RuntimeConfigSnapshot
from apps.gateway.db.models import Base, ConfigRevision
from apps.gateway.db.revisions import RevisionRepository
from apps.gateway.runtime.manager import RuntimeConfigManager


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def sqlite_engine(tmp_path):
    """Real async SQLite engine with all tables created."""
    db_path = tmp_path / "test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(sqlite_engine):
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session


@pytest_asyncio.fixture
def bootstrap_yaml(tmp_path) -> Path:
    """Minimal valid config.yaml for bootstrap path."""
    cfg = tmp_path / "bootstrap.yaml"
    cfg.write_text(textwrap.dedent("""\
        upstreams:
          test-upstream:
            base_url: https://example.test
            auth:
              mode: bearer
              token_env: TEST_TOKEN
        routes:
          default:
            strategy: priority
            candidates:
              - upstream: test-upstream
                model: test-model
                weight: 1
    """), encoding="utf-8")
    return cfg


@pytest_asyncio.fixture
def alt_yaml(tmp_path) -> Path:
    """Alternate config for activation/rollback tests."""
    cfg = tmp_path / "alt.yaml"
    cfg.write_text(textwrap.dedent("""\
        upstreams:
          alt-upstream:
            base_url: https://alt.test
            auth:
              mode: bearer
              token_env: ALT_TOKEN
        routes:
          default:
            strategy: priority
            candidates:
              - upstream: alt-upstream
                model: alt-model
                weight: 1
    """), encoding="utf-8")
    return cfg


# ── startup: load active DB revision ─────────────────────────────────────


@pytest.mark.asyncio
async def test_load_initial_uses_active_db_revision_when_exists(session, bootstrap_yaml):
    """When DB has an active revision, load_initial compiles it — no YAML."""
    # Seed an active revision directly via repo
    repo = RevisionRepository(session)
    seed_data = {
        "connections": {
            "seed-conn": {
                "connection_id": "seed-conn",
                "base_url": "https://seed.test",
                "auth_mode": "bearer",
                "token_env": "SEED_TOKEN",
            }
        },
        "routes": {
            "seed-route": {
                "route_name": "seed-route",
                "strategy": "priority",
                "candidates": [
                    {
                        "upstream": "seed-conn",
                        "model": "seed-model",
                        "weight": 1,
                    }
                ],
                "fallback": [],
                "generated": False,
            }
        },
    }
    draft = await repo.create_draft(seed_data)
    activated = await repo.activate(draft.id)
    await session.commit()

    mgr = RuntimeConfigManager()
    snapshot = await mgr.load_initial(session, bootstrap_path=str(bootstrap_yaml))

    assert mgr.active_revision_id == activated.id
    assert isinstance(snapshot, RuntimeConfigSnapshot)
    # Snapshot must reflect the DB revision, NOT the YAML bootstrap
    assert "seed-conn" in snapshot.connections or "seed-route" in snapshot.routes


@pytest.mark.asyncio
async def test_load_initial_bootstraps_yaml_when_no_active(session, bootstrap_yaml):
    """When DB has no active revision, bootstrap from YAML and persist it."""
    mgr = RuntimeConfigManager()
    snapshot = await mgr.load_initial(session, bootstrap_path=str(bootstrap_yaml))

    assert mgr.active_revision_id is not None
    assert isinstance(snapshot, RuntimeConfigSnapshot)
    # YAML had upstream 'test-upstream' — compiler maps it to a connection
    assert "test-upstream" in snapshot.connections
    assert "default" in snapshot.routes

    # Verify it was persisted as active in DB
    repo = RevisionRepository(session)
    active = await repo.get_active()
    assert active is not None
    assert active.id == mgr.active_revision_id


@pytest.mark.asyncio
async def test_load_initial_does_not_override_existing_active(session, bootstrap_yaml):
    """Existing active revision wins over YAML — config.yaml is bootstrap-only."""
    repo = RevisionRepository(session)
    existing = await repo.create_draft({
        "connections": {
            "existing-conn": {
                "connection_id": "existing-conn",
                "base_url": "https://existing.test",
                "auth_mode": "bearer",
                "token_env": "EXISTING_TOKEN",
            }
        },
        "routes": {},
    })
    await repo.activate(existing.id)
    await session.commit()

    mgr = RuntimeConfigManager()
    await mgr.load_initial(session, bootstrap_path=str(bootstrap_yaml))

    # Must use the existing DB revision, not bootstrap from YAML
    assert mgr.active_revision_id == existing.id


# ── activation: atomic swap ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_activate_swaps_snapshot_atomically(session, bootstrap_yaml, alt_yaml):
    """Activating a new revision atomically replaces the runtime snapshot."""
    from apps.gateway.config.compiler import LegacyConfigCompiler

    # Bootstrap from YAML to get initial active revision
    mgr = RuntimeConfigManager()
    await mgr.load_initial(session, bootstrap_path=str(bootstrap_yaml))
    initial_rev = mgr.active_revision_id
    assert "test-upstream" in mgr.snapshot.connections

    # Create a second revision from alt YAML
    compiler = LegacyConfigCompiler()
    alt_snapshot = compiler.compile_file(str(alt_yaml))
    from apps.gateway.runtime.manager import snapshot_to_dict
    alt_data = snapshot_to_dict(alt_snapshot)

    repo = RevisionRepository(session)
    new_rev = await repo.create_draft(alt_data)
    await session.commit()

    # Activate the new revision
    result = await mgr.activate(new_rev.id, session)
    await session.commit()

    assert mgr.active_revision_id == new_rev.id
    assert mgr.active_revision_id != initial_rev
    assert "alt-upstream" in mgr.snapshot.connections
    assert isinstance(result, RuntimeConfigSnapshot)


@pytest.mark.asyncio
async def test_activate_invalid_revision_preserves_prior(session, bootstrap_yaml):
    """Activating a nonexistent revision must not corrupt current runtime."""
    mgr = RuntimeConfigManager()
    await mgr.load_initial(session, bootstrap_path=str(bootstrap_yaml))
    prior_rev = mgr.active_revision_id
    prior_snapshot = mgr.snapshot

    with pytest.raises(KeyError):
        await mgr.activate("rev_nonexistent", session)

    # Prior snapshot must remain intact
    assert mgr.active_revision_id == prior_rev
    assert mgr.snapshot is prior_snapshot


@pytest.mark.asyncio
async def test_invalid_revision_does_not_replace_current_runtime_or_db_active(session, bootstrap_yaml):
    """Invalid revision must fail before DB activation and before runtime swap."""
    mgr = RuntimeConfigManager()
    await mgr.load_initial(session, bootstrap_path=str(bootstrap_yaml))
    await session.commit()
    prior_rev = mgr.active_revision_id
    prior_snapshot = mgr.snapshot

    repo = RevisionRepository(session)
    invalid = await repo.create_draft({
        "connections": {},
        "routes": {
            "default": {
                "route_name": "default",
                "strategy": "priority",
                "candidates": [
                    {"upstream": "missing-conn", "model": "x", "weight": 1}
                ],
                "fallback": [],
                "generated": False,
            }
        },
    })
    await session.commit()

    with pytest.raises(ValueError, match="unknown connection"):
        await mgr.activate(invalid.id, session)

    assert mgr.active_revision_id == prior_rev
    assert mgr.snapshot is prior_snapshot
    db_active = await repo.get_active()
    assert db_active is not None
    assert db_active.id == prior_rev


# ── rollback ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollback_restores_prior_revision(session, bootstrap_yaml, alt_yaml):
    """Rollback to a prior revision restores its snapshot."""
    from apps.gateway.config.compiler import LegacyConfigCompiler
    from apps.gateway.runtime.manager import snapshot_to_dict

    mgr = RuntimeConfigManager()
    await mgr.load_initial(session, bootstrap_path=str(bootstrap_yaml))
    rev_a = mgr.active_revision_id

    # Create and activate revision B
    compiler = LegacyConfigCompiler()
    alt_snap = compiler.compile_file(str(alt_yaml))
    alt_data = snapshot_to_dict(alt_snap)
    repo = RevisionRepository(session)
    rev_b_obj = await repo.create_draft(alt_data)
    await mgr.activate(rev_b_obj.id, session)
    await session.commit()
    assert mgr.active_revision_id == rev_b_obj.id
    assert "alt-upstream" in mgr.snapshot.connections

    # Rollback to revision A
    await mgr.rollback(rev_a, session)
    await session.commit()

    assert mgr.active_revision_id == rev_a
    assert "test-upstream" in mgr.snapshot.connections


# ── restart persistence ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_persistence(session, bootstrap_yaml, sqlite_engine):
    """After bootstrap + commit, a fresh manager loads the same revision."""
    mgr1 = RuntimeConfigManager()
    await mgr1.load_initial(session, bootstrap_path=str(bootstrap_yaml))
    await session.commit()
    rev_id = mgr1.active_revision_id

    # Simulate restart: new manager, new session from same engine
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as session2:
        mgr2 = RuntimeConfigManager()
        await mgr2.load_initial(session2)
        assert mgr2.active_revision_id == rev_id
        assert mgr2.snapshot is not None
        assert "test-upstream" in mgr2.snapshot.connections


# ── runtime revision observability ───────────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_revision_matches_db_active(session, bootstrap_yaml):
    """Runtime revision ID must equal DB active revision — convergence."""
    mgr = RuntimeConfigManager()
    await mgr.load_initial(session, bootstrap_path=str(bootstrap_yaml))
    await session.commit()

    repo = RevisionRepository(session)
    db_active = await repo.get_active()
    assert db_active is not None
    assert mgr.active_revision_id == db_active.id


@pytest.mark.asyncio
async def test_snapshot_is_immutable_after_activation(session, bootstrap_yaml):
    """After activation, snapshot object must not be mutable."""
    mgr = RuntimeConfigManager()
    snap = await mgr.load_initial(session, bootstrap_path=str(bootstrap_yaml))

    # RuntimeConfigSnapshot is frozen dataclass
    with pytest.raises(AttributeError):
        snap.connections = {}  # type: ignore[misc]


# ── cross-instance convergence (simulated) ───────────────────────────────


@pytest.mark.asyncio
async def test_cross_instance_convergence_after_activate(session, bootstrap_yaml, alt_yaml, sqlite_engine):
    """After activation + commit, a second 'instance' loading from same DB
    converges to the same revision."""
    from apps.gateway.config.compiler import LegacyConfigCompiler
    from apps.gateway.runtime.manager import snapshot_to_dict

    # Instance 1: bootstrap and activate
    mgr1 = RuntimeConfigManager()
    await mgr1.load_initial(session, bootstrap_path=str(bootstrap_yaml))
    await session.commit()

    # Create and activate revision B via instance 1
    compiler = LegacyConfigCompiler()
    alt_snap = compiler.compile_file(str(alt_yaml))
    alt_data = snapshot_to_dict(alt_snap)
    repo = RevisionRepository(session)
    rev_b = await repo.create_draft(alt_data)
    await mgr1.activate(rev_b.id, session)
    await session.commit()

    # Instance 2: fresh manager, fresh session, same DB
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as session2:
        mgr2 = RuntimeConfigManager()
        await mgr2.load_initial(session2)
        # Must converge to the same revision as instance 1
        assert mgr2.active_revision_id == mgr1.active_revision_id
        assert "alt-upstream" in mgr2.snapshot.connections
