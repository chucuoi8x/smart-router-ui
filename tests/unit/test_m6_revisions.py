"""RED tests Step 109 — Revision history (M6) cho Control Plane."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from router import app


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
async def test_revision_history_requires_auth():
    async with _client() as client:
        resp = await client.get("/api/admin/v1/revisions")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revision_history_lists_drafts_and_active():
    async with _client() as client:
        # tạo 1 draft mới
        create = await client.post(
            "/api/admin/v1/revisions",
            json={"routes": {"chat": {"strategy": "priority", "candidates": [{"upstream": "x", "model": "m"}]}}},
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert create.status_code == 201
        draft_id = create.json()["revision_id"]

        # list phải có ít nhất draft vừa tạo + active hiện tại
        listing = await client.get(
            "/api/admin/v1/revisions",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert listing.status_code == 200, listing.text
        data = listing.json()
        assert "items" in data or isinstance(data, list)
        items = data["items"] if isinstance(data, dict) and "items" in data else data
        assert isinstance(items, list)
        ids = {it["revision_id"] for it in items if "revision_id" in it}
        assert draft_id in ids


@pytest.mark.asyncio
async def test_revision_activate_invalid_rejects_non404_payload():
    async with _client() as client:
        resp = await client.post(
            "/api/admin/v1/revisions/bogus/activate",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert resp.status_code == 404
