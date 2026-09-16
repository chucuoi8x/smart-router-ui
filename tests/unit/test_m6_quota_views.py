"""RED tests Step 135 — Quota views AC-05/06."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from router import app

AUTH = {"Authorization": "Bearer test-admin-key"}


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")


@pytest.mark.asyncio
async def test_quota_requires_auth():
    async with _client() as c:
        r = await c.get("/api/admin/v1/quota/resources")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_and_list_quota_resources():
    async with _client() as c:
        payload = {"resource_id": "quota-test-req", "scope": "model/test-model", "metric": "requests", "limit": 100, "window_seconds": 60}
        r = await c.post("/api/admin/v1/quota/resources", json=payload, headers=AUTH)
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["resource_id"] == "quota-test-req"
        assert data["limit"] == 100
        assert "remaining" in data
        # list should contain it
        lst = await c.get("/api/admin/v1/quota/resources", headers=AUTH)
        assert lst.status_code == 200, lst.text
        items = lst.json()["items"]
        assert any(x["resource_id"] == "quota-test-req" for x in items)
        # no secret leak
        assert "sk-" not in lst.text


@pytest.mark.asyncio
async def test_quota_create_validation():
    async with _client() as c:
        r = await c.post("/api/admin/v1/quota/resources", json={"resource_id": ""}, headers=AUTH)
        assert r.status_code == 400
        r2 = await c.post("/api/admin/v1/quota/resources", json={"resource_id": "x", "scope": "s", "metric": "requests", "limit": -1, "window_seconds": 60}, headers=AUTH)
        assert r2.status_code == 400


@pytest.mark.asyncio
async def test_quota_detail_404_for_unknown():
    async with _client() as c:
        r = await c.get("/api/admin/v1/quota/resources/nope", headers=AUTH)
        assert r.status_code == 404
