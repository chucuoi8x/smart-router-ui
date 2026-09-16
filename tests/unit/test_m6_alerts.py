"""RED tests Step 129 — Alerts API AC-13 (M6)."""
import sys
from pathlib import Path
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app
AUTH={"Authorization":"Bearer test-admin-key"}
def _c(): return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")
@pytest.mark.asyncio
async def test_alerts_requires_auth():
    async with _c() as c:
        r=await c.get("/api/admin/v1/alerts")
    assert r.status_code==401
@pytest.mark.asyncio
async def test_create_alert():
    async with _c() as c:
        r=await c.post("/api/admin/v1/alerts",json={"severity":"warning","message":"quota high","source":"quota"},headers=AUTH)
        assert r.status_code==201, r.text
        assert r.json()["severity"]=="warning"
        assert "alert_id" in r.json()
@pytest.mark.asyncio
async def test_list_alerts_contains_created():
    async with _c() as c:
        r=await c.post("/api/admin/v1/alerts",json={"severity":"info","message":"test list"},headers=AUTH)
        aid=r.json()["alert_id"]
        lst=await c.get("/api/admin/v1/alerts",headers=AUTH)
        assert lst.status_code==200
        assert any(x["alert_id"]==aid for x in lst.json()["items"])
@pytest.mark.asyncio
async def test_ack_alert():
    async with _c() as c:
        r=await c.post("/api/admin/v1/alerts",json={"severity":"critical","message":"to ack"},headers=AUTH)
        aid=r.json()["alert_id"]
        p=await c.patch(f"/api/admin/v1/alerts/{aid}",json={"status":"acknowledged"},headers=AUTH)
        assert p.status_code==200, p.text
        assert p.json()["status"]=="acknowledged"
@pytest.mark.asyncio
async def test_alert_unknown_404():
    async with _c() as c:
        r=await c.patch("/api/admin/v1/alerts/nope",json={"status":"acknowledged"},headers=AUTH)
        assert r.status_code==404
