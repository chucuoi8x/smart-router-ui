"""RED tests Step 125 — Xóa credential AC-03."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from router import app

AUTH = {"Authorization": "Bearer test-admin-key"}


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")


async def _create_provider(c):
    r = await c.post(
        "/api/admin/v1/providers",
        json={"template_id": "openai", "name": "cred-del-prov", "base_url": "https://api.openai.com/v1", "api_key": "sk-del-primary"},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    return r.json()["connection_id"]


@pytest.mark.asyncio
async def test_delete_credential_requires_auth():
    async with _client() as c:
        r = await c.delete("/api/admin/v1/providers/whatever/credentials/cred_x")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_credential_unknown_provider_404():
    async with _client() as c:
        r = await c.delete("/api/admin/v1/providers/nope/credentials/cred_x", headers=AUTH)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_credential_unknown_cred_404():
    async with _client() as c:
        cid = await _create_provider(c)
        r = await c.delete(f"/api/admin/v1/providers/{cid}/credentials/cred_unknown", headers=AUTH)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_credential_removes_and_audits():
    async with _client() as c:
        cid = await _create_provider(c)
        added = await c.post(f"/api/admin/v1/providers/{cid}/credentials", json={"alias": "to-delete", "api_key": "sk-to-delete"}, headers=AUTH)
        assert added.status_code == 201, added.text
        cred_id = added.json()["credential_id"]
        assert "sk-to-delete" not in added.text
        r = await c.delete(f"/api/admin/v1/providers/{cid}/credentials/{cred_id}", headers=AUTH)
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] is True
        assert "sk-to-delete" not in r.text
        lst = await c.get(f"/api/admin/v1/providers/{cid}/credentials", headers=AUTH)
        assert lst.status_code == 200
        assert all(x["credential_id"] != cred_id for x in lst.json()["items"])
        audit = await c.get("/api/admin/v1/audit?action=credential.deleted", headers=AUTH)
        assert audit.status_code == 200
        assert any(e.get("credential_id") == cred_id for e in audit.json()["items"])
        assert "sk-to-delete" not in audit.text


@pytest.mark.asyncio
async def test_delete_credential_twice_404():
    async with _client() as c:
        cid = await _create_provider(c)
        added = await c.post(f"/api/admin/v1/providers/{cid}/credentials", json={"alias": "twice", "api_key": "sk-twice"}, headers=AUTH)
        cred_id = added.json()["credential_id"]
        assert (await c.delete(f"/api/admin/v1/providers/{cid}/credentials/{cred_id}", headers=AUTH)).status_code == 200
        r2 = await c.delete(f"/api/admin/v1/providers/{cid}/credentials/{cred_id}", headers=AUTH)
        assert r2.status_code == 404
