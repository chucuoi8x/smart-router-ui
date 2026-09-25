from __future__ import annotations

import httpx

from router import app
import apps.gateway.api.admin as admin_mod
from apps.gateway.providers.driver_context import DriverContext


AUTH = {"Authorization": "Bearer test-admin-key"}


class RecordingDriver:
    """Captures the exact ctx object handed to test and discover calls."""

    driver_id = "recording-driver"
    seen: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def validate_connection(self, ctx):
        RecordingDriver.seen.append(("test", ctx))
        return {"status": "ok"}

    async def discover_models(self, ctx):
        RecordingDriver.seen.append(("discover", ctx))
        return [{"id": "m-1"}]


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


async def _create_provider(c, api_key):
    r = await c.post(
        "/api/admin/v1/providers",
        json={
            "template_id": "openai",
            "name": "recording-prov",
            "base_url": "https://recording.test/v1",
            "api_key": api_key,
            "driver": "recording-driver",
        },
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    return r.json()["connection_id"]


async def test_admin_test_and_discover_receive_driver_context_objects():
    RecordingDriver.seen.clear()
    admin_mod._driver_registry.register("recording-driver", RecordingDriver)
    async with _client() as c:
        cid = await _create_provider(c, "test-key-primary")
        test_r = await c.post(f"/api/admin/v1/providers/{cid}/test", headers=AUTH)
        discover_r = await c.post(f"/api/admin/v1/providers/{cid}/discover", headers=AUTH)

    assert test_r.status_code == 200, test_r.text
    assert discover_r.status_code == 200, discover_r.text
    assert [kind for kind, _ in RecordingDriver.seen] == ["test", "discover"]

    contexts = [ctx for _, ctx in RecordingDriver.seen]
    for ctx in contexts:
        # consolidated construction: real DriverContext, not a raw dict
        assert isinstance(ctx, DriverContext)
        assert ctx.connection_id == cid
        assert ctx.base_url == "https://recording.test/v1"
        assert ctx.driver == "recording-driver"
        assert ctx.credential == {"api_key": "test-key-primary"}
        # legacy dict-style drivers keep working
        assert ctx.get("credential", {}).get("api_key") == "test-key-primary"

    # single source: both endpoints resolved the identical credential row
    assert contexts[0].credential_id == contexts[1].credential_id
    assert contexts[0].credential == contexts[1].credential


async def test_admin_context_credential_matches_explicit_execute_selection():
    """The credential admin test/discover selects must be exactly what the
    data-plane factory resolves for that same credential_id."""
    RecordingDriver.seen.clear()
    admin_mod._driver_registry.register("recording-driver", RecordingDriver)
    async with _client() as c:
        cid = await _create_provider(c, "test-key-primary")
        r = await c.post(f"/api/admin/v1/providers/{cid}/test", headers=AUTH)
        assert r.status_code == 200, r.text

    admin_ctx = RecordingDriver.seen[-1][1]
    assert admin_ctx.credential_id

    from apps.gateway.db.provider_registry import ProviderRegistryRepository
    from apps.gateway.db import session as db_session
    from apps.gateway.providers.driver_context import build_driver_context
    from apps.gateway.db.models import ProviderConnection

    async with db_session.get_async_session_factory()() as session:
        repo = ProviderRegistryRepository(session)
        conn = await session.get(ProviderConnection, cid)
        execute_ctx = await build_driver_context(
            repo, conn, credential_id=admin_ctx.credential_id
        )

    assert execute_ctx.credential == admin_ctx.credential
    assert execute_ctx.credential_id == admin_ctx.credential_id
    assert execute_ctx.connection == admin_ctx.connection
