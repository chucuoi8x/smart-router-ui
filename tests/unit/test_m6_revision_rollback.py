"""RED tests Step 127 — Revision rollback AC-13 (M6)."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from router import app

AUTH = {"Authorization": "Bearer test-admin-key"}


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


async def _make_revision(c, route_id, model):
    draft = await c.post(
        "/api/admin/v1/revisions",
        json={"routes": {route_id: {"strategy": "priority", "candidates": [{"upstream": "u1", "model": model}]}}, "connections": {}},
        headers=AUTH,
    )
    assert draft.status_code == 201, draft.text
    return draft.json()["revision_id"]


@pytest.mark.asyncio
async def test_rollback_requires_auth():
    async with _client() as c:
        r = await c.post("/api/admin/v1/revisions/whatever/rollback")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_rollback_unknown_revision_404():
    async with _client() as c:
        r = await c.post("/api/admin/v1/revisions/nope/rollback", headers=AUTH)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_rollback_reactivates_previous_revision_and_audits():
    async with _client() as c:
        rev_a = await _make_revision(c, "route-a", "model-a")
        await c.post(f"/api/admin/v1/revisions/{rev_a}/activate", headers=AUTH)
        rev_b = await _make_revision(c, "route-b", "model-b")
        await c.post(f"/api/admin/v1/revisions/{rev_b}/activate", headers=AUTH)

        # current active should be route-b
        active_now = await c.get("/api/admin/v1/revisions/active", headers=AUTH)
        assert active_now.json()["revision_id"] == rev_b

        # rollback to rev_a
        r = await c.post(f"/api/admin/v1/revisions/{rev_a}/rollback", headers=AUTH)
        assert r.status_code == 200, r.text
        assert r.json()["revision_id"] == rev_a
        assert r.json()["rolled_back_from"] == rev_b

        # active now equals rev_a; route-b gone, route-a present
        active_after = await c.get("/api/admin/v1/revisions/active", headers=AUTH)
        assert active_after.json()["revision_id"] == rev_a
        routes = (await c.get("/api/admin/v1/routes", headers=AUTH)).json()["items"]
        ids = {x["route_id"] for x in routes}
        assert "route-a" in ids and "route-b" not in ids

        audit = await c.get("/api/admin/v1/audit?action=revision.rolled_back", headers=AUTH)
        assert audit.status_code == 200
        events = audit.json()["items"]
        assert any(e.get("revision_id") == rev_a and e.get("from_revision_id") == rev_b for e in events)


@pytest.mark.asyncio
async def test_rollback_to_current_active_is_400():
    async with _client() as c:
        rev = await _make_revision(c, "route-c", "model-c")
        await c.post(f"/api/admin/v1/revisions/{rev}/activate", headers=AUTH)
        r = await c.post(f"/api/admin/v1/revisions/{rev}/rollback", headers=AUTH)
        assert r.status_code == 400
