"""Final P0: provider probe must never report rejected auth as success."""
import httpx
import pytest

from router import app
import apps.gateway.api.admin as admin
from apps.gateway.providers.generic_openai import GenericOpenAIDriver

AUTH = {"Authorization": "Bearer test-admin-key"}


@pytest.mark.asyncio
async def test_probe_rejected_credential_is_structured_failure(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": {"message": "bad key"}}))
    async with httpx.AsyncClient(transport=transport) as upstream:
        monkeypatch.setattr(admin._driver_registry, "create", lambda driver_id, ctx: GenericOpenAIDriver(client=upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/admin/v1/providers", headers=AUTH, json={"template_id": "openai", "base_url": "https://fake.test/v1", "api_key": "bad-key"})
            cid = created.json()["connection_id"]
            response = await client.post(f"/api/admin/v1/providers/{cid}/test", headers=AUTH)
    assert response.status_code == 401
    body = response.json()
    error = body.get("detail", body)
    assert error["ok"] is False
    assert error["error_kind"] == "AUTH_EXPIRED"
    assert error["message"]
