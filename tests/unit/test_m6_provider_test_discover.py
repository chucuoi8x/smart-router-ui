"""RED tests Step 120 — Provider connection test + model discovery (AC-01)."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from router import app
import apps.gateway.api.admin as admin_mod


AUTH = {"Authorization": "Bearer test-admin-key"}
SECRET = "«redacted:sk-…»"


class FakeTestDriver:
    """Fake provider driver proving test/discover wiring is provider-agnostic."""

    driver_id = "fake-driver"

    def __init__(self, *args, **kwargs):
        pass

    async def validate_connection(self, ctx):
        assert "base_url" in ctx
        return {"status": "ok", "checked": ctx.get("base_url")}

    async def discover_models(self, ctx):
        assert ctx.get("credential", {}).get("api_key") == "***"
        return [{"id": "fake-model-1"}, {"id": "fake-model-2"}]


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


async def _create_fake_provider(c):
    r = await c.post(
        "/api/admin/v1/providers",
        json={
            "template_id": "openai",
            "name": "fake-prov",
            "base_url": "https://fake.test/v1",
            "api_key": "***",
            "driver": "fake-driver",
        },
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    return r.json()["connection_id"]


@pytest.mark.asyncio
async def test_provider_test_requires_auth():
    async with _client() as c:
        r = await c.post("/api/admin/v1/providers/whatever/test")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_provider_test_unknown_connection_404():
    async with _client() as c:
        r = await c.post("/api/admin/v1/providers/nope/test", headers=AUTH)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_provider_test_uses_driver_without_leaking_secret():
    admin_mod._driver_registry.register("fake-driver", FakeTestDriver)
    async with _client() as c:
        cid = await _create_fake_provider(c)
        r = await c.post(f"/api/admin/v1/providers/{cid}/test", headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["connection_id"] == cid
    assert SECRET not in r.text


@pytest.mark.asyncio
async def test_provider_discover_returns_models_without_secret():
    admin_mod._driver_registry.register("fake-driver", FakeTestDriver)
    async with _client() as c:
        cid = await _create_fake_provider(c)
        r = await c.post(f"/api/admin/v1/providers/{cid}/discover", headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    ids = [m["id"] for m in data["models"]]
    assert "fake-model-1" in ids and "fake-model-2" in ids
    assert data["count"] == 2
    assert SECRET not in r.text


@pytest.mark.asyncio
async def test_provider_discover_unknown_connection_404():
    async with _client() as c:
        r = await c.post("/api/admin/v1/providers/nope/discover", headers=AUTH)
    assert r.status_code == 404
