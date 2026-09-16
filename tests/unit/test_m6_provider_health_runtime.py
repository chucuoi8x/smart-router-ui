"""RED tests Step 151 — provider health runtime state AC-07/AC-08."""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app

AUTH = {"Authorization": "Bearer test-admin-key"}


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_health_exposes_rate_limited_runtime_state():
    """A provider that was rate-limited shows short-term retryable state in API response."""
    async with _client() as c:
        created = await c.post(
            "/api/admin/v1/providers",
            json={"template_id":"openai","name":"rl-health","base_url":"https://api.openai.com/v1","api_key":"***"},
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        cid = created.json()["connection_id"]

        # Simulate a rate-limited probe result via the internal cache
        from apps.gateway.api.admin import _provider_health_cache
        from datetime import UTC
        _provider_health_cache[cid] = {
            "status": "degraded",
            "checked_at": "2026-09-16T10:00:00Z",
            "runtime_state": {"state": "rate_limited", "kind": "RATE_LIMIT", "retryable": True, "is_long_term": False},
            "result": {},
        }

        r = await c.get(f"/api/admin/v1/providers/{cid}/health", headers=AUTH)

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "degraded"
    rs = data.get("runtime_state")
    assert isinstance(rs, dict)
    assert rs["state"] == "rate_limited"
    assert rs["retryable"] is True
    assert rs["is_long_term"] is False


@pytest.mark.asyncio
async def test_health_exposes_quota_exhausted_runtime_state():
    """A provider exhausted on quota shows non-retryable long-term state in API response."""
    async with _client() as c:
        created = await c.post(
            "/api/admin/v1/providers",
            json={"template_id":"openai","name":"qe-health","base_url":"https://api.openai.com/v1","api_key":"***"},
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        cid = created.json()["connection_id"]

        from apps.gateway.api.admin import _provider_health_cache
        _provider_health_cache[cid] = {
            "status": "unhealthy",
            "checked_at": "2026-09-16T10:00:00Z",
            "runtime_state": {"state": "quota_exhausted", "kind": "QUOTA_EXHAUSTED", "retryable": False, "is_long_term": True},
            "result": {},
        }

        r = await c.get(f"/api/admin/v1/providers/{cid}/health", headers=AUTH)

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "unhealthy"
    rs = data.get("runtime_state")
    assert isinstance(rs, dict)
    assert rs["state"] == "quota_exhausted"
    assert rs["retryable"] is False
    assert rs["is_long_term"] is True


@pytest.mark.asyncio
async def test_health_summary_includes_runtime_states():
    """The summary endpoint aggregates per-provider runtime states."""
    async with _client() as c:
        created1 = await c.post("/api/admin/v1/providers", json={"template_id":"openai","name":"sum1","base_url":"https://api.openai.com/v1","api_key":"***"}, headers=AUTH)
        assert created1.status_code == 201, created1.text
        cid1 = created1.json()["connection_id"]

        created2 = await c.post("/api/admin/v1/providers", json={"template_id":"openai","name":"sum2","base_url":"https://api.openai.com/v1","api_key":"***"}, headers=AUTH)
        assert created2.status_code == 201, created2.text
        cid2 = created2.json()["connection_id"]

        from apps.gateway.api.admin import _provider_health_cache
        _provider_health_cache[cid1] = {"status": "rate_limited", "checked_at": "2026-09-16T10:00:00Z", "runtime_state": {"state":"rate_limited","kind":"RATE_LIMIT","retryable":True,"is_long_term":False}}
        _provider_health_cache[cid2] = {"status": "unhealthy", "checked_at": "2026-09-16T10:00:00Z", "runtime_state": {"state":"quota_exhausted","kind":"QUOTA_EXHAUSTED","retryable":False,"is_long_term":True}}

        r = await c.get("/api/admin/v1/providers/health", headers=AUTH)

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    cid_map = {i["connection_id"]: i for i in items}
    s1 = cid_map[cid1]["runtime_state"]
    s2 = cid_map[cid2]["runtime_state"]
    assert s1["state"] == "rate_limited"
    assert s2["state"] == "quota_exhausted"
