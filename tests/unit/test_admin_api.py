import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from router import app


@pytest.mark.asyncio
async def test_list_templates():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/api/admin/v1/templates",
            headers={"Authorization": "Bearer test-admin-key"},
        )

    assert response.status_code == 200
    data = response.json()
    assert "openai" in data
    assert "anthropic" in data


@pytest.mark.asyncio
async def test_create_provider_connection_and_revision():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Create a provider connection from template
        payload = {
            "template_id": "openai",
            "name": "My custom OpenAI connection",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-some-mock-key",
        }
        response = await client.post(
            "/api/admin/v1/providers",
            json=payload,
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert response.status_code == 201
        conn = response.json()
        assert conn["name"] == "My custom OpenAI connection"
        assert conn["base_url"] == "https://api.openai.com/v1"
        assert "api_key" not in conn

        # 2. Get the active revision
        response = await client.get(
            "/api/admin/v1/revisions/active",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert response.status_code == 200
        active_rev = response.json()
        assert active_rev is not None

        # 3. Create a revision draft
        payload_rev = {
            "routes": {
                "claude-router-main": {
                    "strategy": "priority",
                    "candidates": [
                        {"upstream": "openai", "model": "gpt-4o", "weight": 1}
                    ],
                }
            }
        }
        response = await client.post(
            "/api/admin/v1/revisions",
            json=payload_rev,
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert response.status_code == 201
        draft = response.json()
        draft_id = draft["revision_id"]

        # 4. Activate the draft revision
        response = await client.post(
            f"/api/admin/v1/revisions/{draft_id}/activate",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert response.status_code == 200

        # 5. Check active revision changed
        response = await client.get(
            "/api/admin/v1/revisions/active",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert response.status_code == 200
        assert response.json()["revision_id"] == draft_id
