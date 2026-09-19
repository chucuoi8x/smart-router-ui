"""P0-16 provider onboarding workflow must be real and DB-backed.

Add → Test → Discover → Create/assign route → Import model → Activate —
all through HTTP endpoints, no restart, no provider-name branching.
"""
from __future__ import annotations

import httpx
import pytest

AUTH = {"Authorization": "Bearer test-admin-key"}


@pytest.mark.asyncio
async def test_onboard_openai_compatible_provider_without_restart(registry_db_url):
    from router import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        created = await c.post(
            "/api/admin/v1/providers",
            json={
                "template_id": "openai",
                "name": "onboard-e2e",
                "base_url": "https://upstream.test/v1",
                "api_key": "***",
                "driver": "generic-openai",
            },
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        cid = created.json()["connection_id"]

        tested = await c.post(f"/api/admin/v1/providers/{cid}/test", headers=AUTH)
        assert tested.status_code == 200, tested.text
        assert tested.json()["ok"] is True

        discovered = await c.post(f"/api/admin/v1/providers/{cid}/discover", headers=AUTH)
        assert discovered.status_code == 200, discovered.text

        route = await c.post(
            "/api/admin/v1/revisions",
            json={"routes": {"onboard-route": {"strategy": "priority", "candidates": []}}},
            headers=AUTH,
        )
        assert route.status_code == 201, route.text
        rev_id = route.json()["revision_id"]
        activated = await c.post(f"/api/admin/v1/revisions/{rev_id}/activate", headers=AUTH)
        assert activated.status_code == 200, activated.text

        imported = await c.post(
            f"/api/admin/v1/providers/{cid}/models/import",
            json={"route_id": "onboard-route", "models": ["gpt-onboard"]},
            headers=AUTH,
        )
        assert imported.status_code == 201, imported.text
        assert imported.json()["imported"] == ["gpt-onboard"]

        routes = await c.get("/api/admin/v1/routes", headers=AUTH)
        assert routes.status_code == 200
        match = [r for r in routes.json()["items"] if r["route_id"] == "onboard-route"]
        assert match and any(
            cand["upstream"] == cid and cand["model"] == "gpt-onboard"
            for cand in match[0]["config"]["candidates"]
        )
