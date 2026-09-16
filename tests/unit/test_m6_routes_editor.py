"""RED tests Step 111 — Route/Policy editor cho Control Plane (M6)."""

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
async def test_routes_editor_requires_auth():
    async with _client() as client:
        response = await client.get("/api/admin/v1/routes")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_routes_editor_lists_and_updates_route():
    async with _client() as client:
        headers = {"Authorization": "Bearer test-admin-key"}
        # tạo 1 route mới qua revision
        payload = {"routes": {"route-editor-test": {"strategy": "priority", "candidates": []}}}
        created = await client.post("/api/admin/v1/revisions", json=payload, headers=headers)
        assert created.status_code == 201
        revision_id = created.json()["revision_id"]
        activated = await client.post(f"/api/admin/v1/revisions/{revision_id}/activate", headers=headers)
        assert activated.status_code == 200

        listed = await client.get("/api/admin/v1/routes", headers=headers)
        assert listed.status_code == 200, listed.text
        routes = listed.json()
        items = routes["items"] if isinstance(routes, dict) and "items" in routes else routes
        assert any(r.get("route_id") == "route-editor-test" for r in items)

        updated = await client.put(
            "/api/admin/v1/routes/route-editor-test",
            json={"strategy": "smart", "candidates": [{"upstream": "x", "model": "m"}]},
            headers=headers,
        )
        assert updated.status_code == 200, updated.text
        assert "sk-" not in updated.text
        assert "api_key" not in updated.text.lower()
        audit = await client.get("/api/admin/v1/audit?action=route.updated", headers=headers)
        assert audit.status_code == 200
        assert any(e.get("route_id") == "route-editor-test" for e in audit.json()["items"])


@pytest.mark.asyncio
async def test_routes_editor_validates_unknown_route():
    async with _client() as client:
        response = await client.put(
            "/api/admin/v1/routes/nonexistent-route",
            json={"strategy": "smart"},
            headers={"Authorization": "Bearer test-admin-key"},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_routes_editor_validates_invalid_strategy():
    async with _client() as client:
        await client.post(
            "/api/admin/v1/revisions",
            json={"routes": {"route-invalid-test": {"candidates": []}}},
            headers={"Authorization": "Bearer test-admin-key"},
        )
        valid = (
            await client.post(
                "/api/admin/v1/revisions",
                json={"routes": {"route-invalid-test": {"strategy": "priority", "candidates": []}}},
                headers={"Authorization": "Bearer test-admin-key"},
            )
        ).json()["revision_id"]
        await client.post(
            f"/api/admin/v1/revisions/{valid}/activate",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        response = await client.put(
            "/api/admin/v1/routes/route-invalid-test",
            json={"strategy": "not-a-strategy"},
            headers={"Authorization": "Bearer test-admin-key"},
        )
    assert response.status_code == 400
