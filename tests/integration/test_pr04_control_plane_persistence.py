"""PR-04: Control Plane state survives restart (durable truth, not dicts)."""
from __future__ import annotations

import asyncio
import os

import pytest


@pytest.mark.asyncio
async def test_project_key_budget_policy_audit_survive_engine_restart():
    import apps.gateway.db.session as db_session
    from apps.gateway.db.control_plane import ControlPlaneRepository
    from apps.gateway.db.models import Base

    url = os.environ["DATABASE_URL"]

    async with db_session.get_async_session_factory()() as session:
        repo = ControlPlaneRepository(session)
        project = await repo.create_project(
            name="restart-proj", description="d", secret_key_hash="enc:x"
        )
        key = await repo.create_project_key(
            project_id=project.id, alias="default", secret_encrypted="enc:k"
        )
        await repo.upsert_budget(
            project_id=project.id, currency="USD", ceiling=100.0, used=20.0,
            allow_paid_fallback=False,
        )
        await repo.set_policy(
            "paid-fallback", enabled=True, requires_budget=True, project_id=project.id
        )
        await repo.set_setting("log_level", "DEBUG")
        await repo.create_alert(severity="critical", message="disk", source="system")
        await repo.add_audit_event("project.created", {"project_id": project.id})
        await session.commit()
        project_id, key_id = project.id, key.id

    # Simulate process restart: dispose engine, rebuild schema bindings from
    # the SAME file (create_all is a no-op when tables exist).
    await db_session.dispose_engine()
    engine = db_session.init_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with db_session.get_async_session_factory()() as session:
        repo = ControlPlaneRepository(session)
        assert await repo.get_project(project_id) is not None
        keys = await repo.list_project_keys(project_id)
        assert [k.id for k in keys] == [key_id]
        budget = await repo.get_budget(project_id)
        assert budget is not None and budget.ceiling == 100.0
        assert budget.allow_paid_fallback is False
        policy = await repo.get_policy("paid-fallback")
        assert policy["enabled"] is True and policy["project_id"] == project_id
        settings = await repo.get_settings()
        assert settings["log_level"] == "DEBUG"
        alerts = await repo.list_alerts(severity="critical")
        assert len(alerts) == 1
        events = await repo.list_audit_events(action="project.created")
        assert len(events) == 1
        assert events[0].metadata_["project_id"] == project_id


@pytest.mark.asyncio
async def test_policy_defaults_persist_without_row():
    from apps.gateway.db.control_plane import ControlPlaneRepository
    from apps.gateway.db.session import get_async_session_factory

    async with get_async_session_factory()() as session:
        repo = ControlPlaneRepository(session)
        policy = await repo.get_policy("paid-fallback")
        assert policy == {
            "policy": "paid-fallback",
            "enabled": False,
            "requires_budget": True,
            "project_id": None,
        }


@pytest.mark.asyncio
async def test_revoke_key_updates_durable_state():
    from apps.gateway.db.control_plane import ControlPlaneRepository
    from apps.gateway.db.session import get_async_session_factory

    async with get_async_session_factory()() as session:
        repo = ControlPlaneRepository(session)
        project = await repo.create_project(
            name="p", description="", secret_key_hash="enc:x"
        )
        key = await repo.create_project_key(
            project_id=project.id, alias="a", secret_encrypted="enc:y"
        )
        await session.commit()
        assert key.is_active is True
        revoked = await repo.revoke_project_key(key)
        await session.commit()
        assert revoked.is_active is False
        assert revoked.revoked_at is not None
        # reload proves durable write, not in-object mutation
        reloaded = await repo.get_project_key(project.id, key.id)
        assert reloaded is not None and reloaded.is_active is False
