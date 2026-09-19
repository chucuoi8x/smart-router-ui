"""PR-07: Config revision validation + activation + rollback.

Real validation: invalid config cannot activate.
Atomic activation: compile + validate before swap.
Last-known-good: invalid config keeps current active revision.
Rollback: revert to previous revision.
"""
import pytest
import httpx
from router import app


@pytest.mark.asyncio
async def test_invalid_config_cannot_activate():
    """Invalid config (missing upstream) must fail validation and not activate."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Create a valid provider first
        r = await client.post(
            "/api/admin/v1/providers",
            json={"template_id": "openai", "name": "test", "base_url": "https://api.openai.com/v1", "api_key": "sk-test"},
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 201
        conn_id = r.json()["connection_id"]

        # Try to activate invalid config (references non-existent upstream)
        invalid_snapshot = {
            "connections": {conn_id: {}},
            "routes": {
                "test-route": {
                    "strategy": "priority",
                    "candidates": [{"upstream": "non-existent-upstream", "model": "gpt-4", "weight": 1}],
                    "fallback": [],
                }
            }
        }
        r = await client.post(
            "/api/admin/v1/revisions",
            json=invalid_snapshot,
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 201
        draft_id = r.json()["revision_id"]
        r = await client.post(
            f"/api/admin/v1/revisions/{draft_id}/activate",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 400
        assert "upstream" in r.text.lower() or "not found" in r.text.lower()


@pytest.mark.asyncio
async def test_last_known_good_preserved_on_invalid():
    """Invalid config must not replace last-known-good revision."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Create valid provider
        r = await client.post(
            "/api/admin/v1/providers",
            json={"template_id": "openai", "name": "test", "base_url": "https://api.openai.com/v1", "api_key": "sk-test"},
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 201
        conn_id = r.json()["connection_id"]

        # Activate valid config
        valid_snapshot = {
            "connections": {conn_id: {}},
            "routes": {
                "test-route": {
                    "strategy": "priority",
                    "candidates": [{"upstream": conn_id, "model": "gpt-4", "weight": 1}],
                    "fallback": [],
                }
            }
        }
        r = await client.post(
            "/api/admin/v1/revisions",
            json=valid_snapshot,
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 201
        valid_rev_id = r.json()["revision_id"]
        r = await client.post(
            f"/api/admin/v1/revisions/{valid_rev_id}/activate",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 200

        # Get current active
        r = await client.get(
            "/api/admin/v1/revisions/active",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 200
        active_before = r.json()["revision_id"]
        assert active_before == valid_rev_id

        # Try invalid config
        invalid_snapshot = {
            "connections": {conn_id: {}},
            "routes": {
                "bad-route": {
                    "strategy": "priority",
                    "candidates": [{"upstream": "does-not-exist", "model": "gpt-4", "weight": 1}],
                    "fallback": [],
                }
            }
        }
        r = await client.post(
            "/api/admin/v1/revisions",
            json=invalid_snapshot,
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 201
        bad_rev_id = r.json()["revision_id"]
        r = await client.post(
            f"/api/admin/v1/revisions/{bad_rev_id}/activate",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 400

        # Active revision must still be the valid one
        r = await client.get(
            "/api/admin/v1/revisions/active",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 200
        active_after = r.json()["revision_id"]
        assert active_after == valid_rev_id


@pytest.mark.asyncio
async def test_atomic_activation():
    """Activation must compile + validate before swapping active revision."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Create provider
        r = await client.post(
            "/api/admin/v1/providers",
            json={"template_id": "openai", "name": "test", "base_url": "https://api.openai.com/v1", "api_key": "sk-test"},
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 201
        conn_id = r.json()["connection_id"]

        # Valid config should activate successfully
        valid_snapshot = {
            "routes": {
                "test-route": {
                    "strategy": "priority",
                    "candidates": [{"upstream": conn_id, "model": "gpt-4", "weight": 1}],
                    "fallback": [],
                }
            }
        }
        r = await client.post(
            "/api/admin/v1/revisions",
            json=valid_snapshot,
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 201
        rev_id = r.json()["revision_id"]
        r = await client.post(
            f"/api/admin/v1/revisions/{rev_id}/activate",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 200

        # Verify it's active
        r = await client.get(
            "/api/admin/v1/revisions/active",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 200
        assert r.json()["revision_id"] == rev_id


@pytest.mark.asyncio
async def test_rollback_to_previous_revision():
    """Rollback endpoint must revert to previous revision."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Create provider
        r = await client.post(
            "/api/admin/v1/providers",
            json={"template_id": "openai", "name": "test", "base_url": "https://api.openai.com/v1", "api_key": "sk-test"},
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 201
        conn_id = r.json()["connection_id"]

        # Activate first valid config
        snapshot1 = {
            "connections": {conn_id: {}},
            "routes": {
                "route1": {
                    "strategy": "priority",
                    "candidates": [{"upstream": conn_id, "model": "gpt-4", "weight": 1}],
                    "fallback": [],
                }
            }
        }
        r = await client.post(
            "/api/admin/v1/revisions",
            json=snapshot1,
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 201
        rev1_id = r.json()["revision_id"]
        r = await client.post(
            f"/api/admin/v1/revisions/{rev1_id}/activate",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 200

        # Activate second valid config
        snapshot2 = {
            "connections": {conn_id: {}},
            "routes": {
                "route2": {
                    "strategy": "priority",
                    "candidates": [{"upstream": conn_id, "model": "gpt-4o", "weight": 1}],
                    "fallback": [],
                }
            }
        }
        r = await client.post(
            "/api/admin/v1/revisions",
            json=snapshot2,
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 201
        rev2_id = r.json()["revision_id"]
        r = await client.post(
            f"/api/admin/v1/revisions/{rev2_id}/activate",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 200

        # Verify rev2 is active
        r = await client.get(
            "/api/admin/v1/revisions/active",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 200
        assert r.json()["revision_id"] == rev2_id

        # Rollback to previous
        r = await client.post(
            f"/api/admin/v1/revisions/{rev1_id}/rollback",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 200
        rolled_back_rev_id = r.json()["revision_id"]
        assert rolled_back_rev_id == rev1_id

        # Verify rollback succeeded
        r = await client.get(
            "/api/admin/v1/revisions/active",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert r.status_code == 200
        assert r.json()["revision_id"] == rev1_id
