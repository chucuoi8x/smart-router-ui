"""RED tests Step 114 — Revision detail cho Control Plane."""

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
async def test_revision_detail_requires_auth():
    async with _client() as c:
        r = await c.get("/api/admin/v1/revisions/dummy")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_revision_detail_returns_serialized_revision():
    async with _client() as c:
        h = {"Authorization": "Bearer test-admin-key"}
        created = await c.post(
            "/api/admin/v1/revisions",
            json={"routes": {"rev-detail-test": {"strategy": "priority"}}},
            headers=h,
        )
        rid = created.json()["revision_id"]

        resp = await c.get(f"/api/admin/v1/revisions/{rid}", headers=h)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["revision_id"] == rid
        assert "snapshot_data" in body or "config" in body or "routes" in str(body).lower()
        # không lộ secret
        assert "sk-" not in resp.text


@pytest.mark.asyncio
async def test_revision_detail_404():
    async with _client() as c:
        r = await c.get(
            "/api/admin/v1/revisions/bogus-12345",
            headers={"Authorization": "Bearer test-admin-key"},
        )
    assert r.status_code == 404
