"""A new API key update must never rewrite legacy credential storage."""
import httpx
import pytest
from apps.gateway.db.dependencies import get_session
from apps.gateway.db.models import ProviderConnection

AUTH = {"Authorization": "Bearer test-admin-key"}


@pytest.mark.asyncio
async def test_provider_update_writes_only_canonical_credential_row():
    from router import app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        created = await c.post("/api/admin/v1/providers", headers=AUTH, json={
            "template_id": "openai", "name": "update-canonical",
            "base_url": "https://provider.invalid/v1", "api_key": "old-key",
        })
        cid = created.json()["connection_id"]
        updated = await c.put(f"/api/admin/v1/providers/{cid}", headers=AUTH, json={"api_key": "new-key"})
        assert updated.status_code == 200
        gen = get_session()
        session = await gen.__anext__()
        try:
            conn = await session.get(ProviderConnection, cid)
        finally:
            await gen.aclose()
    assert conn is not None
    assert conn.credential_encrypted is None
