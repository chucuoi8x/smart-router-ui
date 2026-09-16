"""RED tests Step 108 — Overview Control Plane API (M6)."""
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
async def test_overview_requires_admin_auth():
    async with _client() as client:
        resp = await client.get("/api/admin/v1/overview")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_overview_returns_summary_with_admin_auth():
    async with _client() as client:
        resp = await client.get(
            "/api/admin/v1/overview",
            headers={"Authorization": "Bearer test-admin-key"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # cấu trúc tối thiểu cho Overview panel — không lộ secret
    assert "providers" in data
    assert "revisions" in data
    assert "usage" in data or "ledger" in data or "stats" in data
    assert "health" in data or "status" in data
    # không lộ api_key / secret
    raw = resp.text.lower()
    assert "api_key" not in raw or "credential_present" in raw  # cho phép flag boolean
    # providers là list/dict, không chứa secret plaintext
    for key in ["sk-", "ghp_"]:
        assert key not in resp.text


@pytest.mark.asyncio
async def test_overview_wrong_token_rejected():
    async with _client() as client:
        resp = await client.get(
            "/api/admin/v1/overview",
            headers={"Authorization": "Bearer wrong"},
        )
    assert resp.status_code == 401
