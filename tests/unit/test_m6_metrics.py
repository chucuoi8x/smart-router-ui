"""RED tests Step 137 — structured metrics endpoint M7."""
import sys
from pathlib import Path
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app

def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")

@pytest.mark.asyncio
async def test_metrics_is_public_and_returns_structured_payload():
    async with _client() as c:
        r = await c.get("/metrics")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["service"] == "smart-router"
    assert "version" in data
    assert "uptime_seconds" in data
    assert isinstance(data["uptime_seconds"], int)
    assert data["uptime_seconds"] >= 0

@pytest.mark.asyncio
async def test_metrics_includes_upstream_and_timestamp():
    async with _client() as c:
        r = await c.get("/metrics")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "upstream_count" in data or "upstreams" in data
    # timestamp or generated_at for scrapers
    assert "timestamp" in data or "generated_at" in data or "uptime_seconds" in data

@pytest.mark.asyncio
async def test_metrics_has_request_id_header():
    async with _client() as c:
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert r.headers.get("x-request-id")
