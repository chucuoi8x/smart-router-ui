"""RED tests Step 113 — Xóa provider an toàn + audit (M6)."""
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
async def test_delete_provider_requires_auth():
    async with _client() as c:
        r = await c.delete("/api/admin/v1/providers/conn_x")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_provider_removes_it_and_audits():
    async with _client() as c:
        h = {"Authorization": "Bearer test-admin-key"}
        created = await c.post(
            "/api/admin/v1/providers",
            json={"template_id": "openai", "name": "prov-del", "base_url": "https://api.openai.com/v1", "api_key": "sk-del"},
            headers=h,
        )
        assert created.status_code == 201
        cid = created.json()["connection_id"]

        deleted = await c.delete(f"/api/admin/v1/providers/{cid}", headers=h)
        assert deleted.status_code == 200, deleted.text

        gone = await c.get(f"/api/admin/v1/providers/{cid}", headers=h)
        assert gone.status_code == 404

        listed = await c.get("/api/admin/v1/providers", headers=h)
        assert cid not in {x["connection_id"] for x in listed.json()["items"]}

        audit = await c.get("/api/admin/v1/audit?action=provider.deleted", headers=h)
        assert audit.status_code == 200
        assert any(e.get("connection_id") == cid for e in audit.json()["items"])
        assert "sk-del" not in audit.text
        assert "api_key" not in audit.text.lower()


@pytest.mark.asyncio
async def test_delete_unknown_provider_404():
    async with _client() as c:
        r = await c.delete("/api/admin/v1/providers/nope", headers={"Authorization": "Bearer test-admin-key"})
    assert r.status_code == 404
