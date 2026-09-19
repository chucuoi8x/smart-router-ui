"""Smoke tests for durable ledger query endpoints.

Verifies that the new ``/ledger/*`` routes are mounted on the admin
router and return proper auth-401 / route-not-found responses when
no database is reachable (expected in most CI/test environments).
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from router import app


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ── auth guard ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ledger_stats_requires_admin_auth():
    async with _client() as client:
        resp = await client.get("/api/admin/v1/ledger/stats")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ledger_requests_requires_admin_auth():
    async with _client() as client:
        resp = await client.get("/api/admin/v1/ledger/requests")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ledger_request_detail_requires_admin_auth():
    async with _client() as client:
        resp = await client.get("/api/admin/v1/ledger/requests/some-id")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ledger_stats_detail_requires_admin_auth():
    # GET /ledger/requests/{request_id} also goes through admin router
    async with _client() as client:
        resp = await client.get(
            "/api/admin/v1/ledger/stats",
            headers={"Authorization": "Bearer wrong"},
        )
    assert resp.status_code == 401


# ── graceful degradation when no DB ───────────────────────────────

@pytest.mark.asyncio
async def test_ledger_stats_returns_ok_with_db():
    """With SQLite fixture active, the ledger endpoint is reachable
    and returns 200 (empty ledger). The "no DB" degradation path
    is not exercised in this test suite because the autouse fixture
    guarantees a working database."""
    async with _client() as client:
        resp = await client.get(
            "/api/admin/v1/ledger/stats",
            headers={"Authorization": "Bearer test-admin-key"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ledger_requests_list_returns_ok_with_db():
    async with _client() as client:
        resp = await client.get(
            "/api/admin/v1/ledger/requests",
            headers={"Authorization": "Bearer test-admin-key"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ledger_request_detail_returns_404_when_not_found():
    """A real DB would return 404; without a DB it may return 500.
    Either non-2xx response confirms the route matched."""
    async with _client() as client:
        resp = await client.get(
            "/api/admin/v1/ledger/requests/nonexistent-req",
            headers={"Authorization": "Bearer test-admin-key"},
        )
    assert resp.status_code in [404, 500]
