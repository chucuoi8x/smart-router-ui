"""RED tests Step 136 — health/readiness uptime + dependency checks (AC-15/M7)."""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from router import app


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")


@pytest.mark.asyncio
async def test_live_includes_uptime_and_version():
    async with _client() as c:
        r = await c.get("/health/live")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "ok"
    assert data["service"] == "smart-router"
    assert "version" in data
    assert "uptime_seconds" in data
    assert isinstance(data["uptime_seconds"], int)
    assert data["uptime_seconds"] >= 0
    assert r.headers.get("x-request-id")


@pytest.mark.asyncio
async def test_ready_includes_upstream_and_checks():
    async with _client() as c:
        r = await c.get("/health/ready")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "ok"
    assert "upstream_count" in data
    assert isinstance(data["upstream_count"], int)
    assert "checks" in data
    checks = data["checks"]
    # at minimum upstreams + database/redis keys documented in AC-15
    assert "upstreams" in checks or "database" in checks or "redis" in checks


@pytest.mark.asyncio
async def test_healthz_backward_compat_plus_uptime():
    async with _client() as c:
        r = await c.get("/healthz")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "ok"
    assert "uptime_seconds" in data or "version" in data
