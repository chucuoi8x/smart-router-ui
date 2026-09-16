"""RED tests Step 119 — Legacy YAML migration AC-16."""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from router import app


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_migration_yaml_requires_auth():
    async with _client() as c:
        r = await c.post("/api/admin/v1/migration/yaml", data={"yaml_data": "routes: {}"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_migration_yaml_compiles_and_activates_revision():
    yaml_data = """
upstreams:
  proxypal:
    base_url: "http://127.0.0.1:8317"
    auth:
      mode: bearer
      token_env: PROXYPAL_KEY
routes:
  main-route:
    strategy: priority
    candidates:
      - upstream: proxypal
        model: gpt-5.5
    fallback:
      - upstream: proxypal
        model: gemini-flash
"""
    async with _client() as c:
        h = {"Authorization": "Bearer test-admin-key"}
        r = await c.post("/api/admin/v1/migration/yaml", data={"yaml_data": yaml_data}, headers=h)
        assert r.status_code == 201, r.text
        data = r.json()
        assert "revision_id" in data
        rev_id = data["revision_id"]
        # verify the revision was activated
        assert data.get("activated") is True
        # verify routes were compiled (reuse same client so session stays open)
        details = await c.get(f"/api/admin/v1/revisions/{rev_id}", headers=h)
    detail = details.json()
    snap = detail.get("snapshot_data", {})
    routes = snap.get("routes", {})
    main = routes.get("main-route", {})
    cands = main.get("candidates", [])
    assert len(cands) >= 1
    assert any(c.get("model") == "gpt-5.5" for c in cands)
    fb = main.get("fallback", [])
    assert any(c.get("model") == "gemini-flash" for c in fb)
    # verify connection was created
    conns = snap.get("connections", {})
    assert "proxypal" in conns


@pytest.mark.asyncio
async def test_migration_yaml_validates_syntax():
    bad_yaml = "routes:\n  -: invalid\n    strategy:"
    async with _client() as c:
        h = {"Authorization": "Bearer test-admin-key"}
        r = await c.post("/api/admin/v1/migration/yaml", data={"yaml_data": bad_yaml}, headers=h)
    # should fail gracefully (validation error)
    assert r.status_code == 400
