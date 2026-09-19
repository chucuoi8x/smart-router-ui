import sys
from pathlib import Path
import pytest
import httpx
from router import app
import apps.gateway.api.admin as admin_mod

# Auth token for test admin
AUTH = {"Authorization": "Bearer test-admin-key"}

@pytest.mark.asyncio
async def test_e2e_dynamic_provider_onboarding_success():
    # 1. Create provider connection
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver") as c:
        # Create Provider
        r = await c.post(
            "/api/admin/v1/providers",
            json={
                "template_id": "openai",
                "name": "e2e-provider",
                "base_url": "https://api.example.test/v1",
                "api_key": "sk-12345",
                "driver": "generic-openai",
            },
            headers=AUTH,
        )
        assert r.status_code == 201, r.text
        cid = r.json()["connection_id"]

        # 2. Test real mock endpoint
        r = await c.post(f"/api/admin/v1/providers/{cid}/test", headers=AUTH)
        assert r.status_code == 200, r.text

        # 3. Discover models
        r = await c.post(f"/api/admin/v1/providers/{cid}/discover", headers=AUTH)
        assert r.status_code == 200, r.text
        
        # 4. Import model
        r = await c.post(f"/api/admin/v1/providers/{cid}/models/import", 
                         json={"route_id": "test-route", "models": ["gpt-4o"]},
                         headers=AUTH)
        # Should be 201 or 200
        assert r.status_code in (200, 201), r.text
