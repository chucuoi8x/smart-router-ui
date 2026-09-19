"""P0-05: config revision state must survive process restart.

Plan invariant 3.4: PostgreSQL is durable truth.  The active revision pointer
and every draft must live in the database, not in a process-local dict.
"""
import httpx
import pytest
from sqlalchemy import select

from apps.gateway.db.models import ConfigRevision
from apps.gateway.db.session import dispose_engine, init_engine

AUTH = {"Authorization": "Bearer test-admin-key"}


@pytest.mark.asyncio
async def test_active_revision_survives_engine_restart(registry_db_url):
    """Activate a revision, dispose the engine, rebuild it — active revision must still be N."""
    from router import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        draft = await c.post(
            "/api/admin/v1/revisions",
            json={"routes": {"restart-test": {"strategy": "priority", "candidates": []}}},
            headers=AUTH,
        )
        assert draft.status_code == 201, draft.text
        rev_id = draft.json()["revision_id"]

        activated = await c.post(f"/api/admin/v1/revisions/{rev_id}/activate", headers=AUTH)
        assert activated.status_code == 200, activated.text

    # Simulate process restart: dispose engine, rebuild against same DB
    await dispose_engine()
    init_engine(registry_db_url)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        active = await c.get("/api/admin/v1/revisions/active", headers=AUTH)
        assert active.status_code == 200, active.text
        assert active.json()["revision_id"] == rev_id

        # Verify DB directly
        from apps.gateway.db.session import get_async_session_factory
        async with get_async_session_factory()() as session:
            result = await session.execute(select(ConfigRevision).where(ConfigRevision.id == rev_id))
            row = result.scalar_one()
            assert row.is_active is True
            assert row.activated_at is not None


@pytest.mark.asyncio
async def test_invalid_activation_preserves_last_known_good(registry_db_url):
    """Attempt to activate an invalid revision — last-known-good must remain active."""
    from router import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        # Create and activate a valid revision
        valid_draft = await c.post(
            "/api/admin/v1/revisions",
            json={"routes": {"good-route": {"strategy": "priority", "candidates": []}}},
            headers=AUTH,
        )
        assert valid_draft.status_code == 201
        good_rev_id = valid_draft.json()["revision_id"]

        activated = await c.post(f"/api/admin/v1/revisions/{good_rev_id}/activate", headers=AUTH)
        assert activated.status_code == 200

        # Create an invalid revision (bad strategy)
        invalid_draft = await c.post(
            "/api/admin/v1/revisions",
            json={"routes": {"bad-route": {"strategy": "invalid-strategy", "candidates": []}}},
            headers=AUTH,
        )
        assert invalid_draft.status_code == 201
        bad_rev_id = invalid_draft.json()["revision_id"]

        # Attempt to activate invalid revision — must fail
        bad_activate = await c.post(f"/api/admin/v1/revisions/{bad_rev_id}/activate", headers=AUTH)
        assert bad_activate.status_code == 400, bad_activate.text

        # Active revision must still be the good one
        active = await c.get("/api/admin/v1/revisions/active", headers=AUTH)
        assert active.status_code == 200
        assert active.json()["revision_id"] == good_rev_id
