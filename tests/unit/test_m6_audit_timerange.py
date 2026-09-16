"""RED tests Step 143 — audit time-range filter (M7)."""
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app

AUTH = {"Authorization": "Bearer test-admin-key"}

def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")

@pytest.mark.asyncio
async def test_audit_since_filters_future_returns_empty():
    async with _client() as c:
        # ensure at least one event exists
        draft = await c.post("/api/admin/v1/revisions", json={"routes": {"tr-test": {"candidates": []}}}, headers=AUTH)
        assert draft.status_code == 201, draft.text
        rid = draft.json()["revision_id"]
        await c.post(f"/api/admin/v1/revisions/{rid}/activate", headers=AUTH)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        r = await c.get(f"/api/admin/v1/audit?since={future}", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0
    assert r.json()["items"] == []

@pytest.mark.asyncio
async def test_audit_since_filters_past_returns_events():
    async with _client() as c:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        r = await c.get(f"/api/admin/v1/audit?since={past}", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["total"] >= 1

@pytest.mark.asyncio
async def test_audit_export_since_filters():
    async with _client() as c:
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        r = await c.get(f"/api/admin/v1/audit/export?since={future}", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0
    assert r.json()["items"] == []
    assert "exported_at" in r.json()

@pytest.mark.asyncio
async def test_audit_invalid_since_returns_400():
    async with _client() as c:
        r = await c.get("/api/admin/v1/audit?since=not-a-date", headers=AUTH)
    assert r.status_code == 400
