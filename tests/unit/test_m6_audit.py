"""RED tests Step 110 — audit history cho Control Plane revisions."""

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
async def test_audit_endpoint_requires_auth():
    async with _client() as client:
        response = await client.get("/api/admin/v1/audit")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_revision_activation_creates_redacted_audit_event():
    async with _client() as client:
        headers = {"Authorization": "Bearer test-admin-key"}
        draft = await client.post(
            "/api/admin/v1/revisions",
            json={"routes": {"audit-test": {"candidates": []}}, "api_key": "sk-should-not-leak"},
            headers=headers,
        )
        assert draft.status_code == 201
        revision_id = draft.json()["revision_id"]

        activated = await client.post(
            f"/api/admin/v1/revisions/{revision_id}/activate",
            headers=headers,
        )
        assert activated.status_code == 200

        audit = await client.get("/api/admin/v1/audit", headers=headers)
        assert audit.status_code == 200, audit.text
        data = audit.json()
        assert data["total"] >= 1
        events = data["items"]
        assert any(
            event.get("action") == "revision.activated"
            and event.get("revision_id") == revision_id
            for event in events
        )
        assert "sk-should-not-leak" not in audit.text
        assert "api_key" not in audit.text.lower()


@pytest.mark.asyncio
async def test_audit_supports_action_filter():
    async with _client() as client:
        response = await client.get(
            "/api/admin/v1/audit?action=revision.activated",
            headers={"Authorization": "Bearer test-admin-key"},
        )
    assert response.status_code == 200
    assert all(item["action"] == "revision.activated" for item in response.json()["items"])
