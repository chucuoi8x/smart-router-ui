import httpx
import pytest

from router import app
import apps.gateway.api.admin as admin

AUTH = {"Authorization": "Bearer test-admin-key"}


class FailingDiscoveryDriver:
    def __init__(self, *args, **kwargs):
        pass

    async def discover_models(self, ctx):
        raise admin.ProviderDiscoveryError("AUTH_EXPIRED", "provider rejected credential", 401)


@pytest.mark.asyncio
async def test_discovery_error_returns_structured_http_failure(monkeypatch):
    admin._driver_registry.register("failing-discovery", FailingDiscoveryDriver)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/admin/v1/providers", headers=AUTH, json={
            "template_id": "openai", "driver": "failing-discovery",
            "base_url": "https://provider.invalid/v1", "api_key": "key",
        })
        assert created.status_code == 201
        response = await client.post(
            f"/api/admin/v1/providers/{created.json()['connection_id']}/discover", headers=AUTH
        )
    assert response.status_code == 401
    assert response.json()["ok"] is False
    assert response.json()["error_kind"] == "AUTH_EXPIRED"
