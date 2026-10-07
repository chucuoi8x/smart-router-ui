"""P0 §20 runtime authority integration tests.

These tests exercise the real FastAPI lifespan and router state, not only the
RuntimeConfigManager unit seam.  No provider network calls are made.
"""
from __future__ import annotations

import os

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from apps.gateway.db.models import Base
from apps.gateway.db.revisions import RevisionRepository


@pytest.mark.asyncio
async def test_lifespan_loads_active_db_revision_into_live_router(tmp_path, monkeypatch):
    """Active DB revision controls live SmartRouter; config.yaml stays bootstrap-only."""
    db_path = tmp_path / "runtime_authority.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        repo = RevisionRepository(session)
        rev = await repo.create_draft(
            {
                "connections": {
                    "db-conn": {
                        "connection_id": "db-conn",
                        "base_url": "https://db-revision.test",
                        "auth_mode": "bearer",
                        "token_env": "DB_REVISION_TOKEN",
                    }
                },
                "routes": {
                    "db-route": {
                        "route_name": "db-route",
                        "strategy": "priority",
                        "candidates": [
                            {"upstream": "db-conn", "model": "db-model", "weight": 1}
                        ],
                        "fallback": [],
                        "generated": False,
                    }
                },
            }
        )
        await repo.activate(rev.id)
        await session.commit()

    bootstrap = tmp_path / "config.yaml"
    bootstrap.write_text(
        """
upstreams:
  yaml-conn:
    base_url: https://yaml-bootstrap.test
    auth:
      mode: bearer
      token_env: YAML_TOKEN
routes:
  yaml-route:
    strategy: priority
    candidates:
      - upstream: yaml-conn
        model: yaml-model
        weight: 1
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SMART_ROUTER_CONFIG", str(bootstrap))
    monkeypatch.setenv("SMART_ROUTER_ENCRYPTION_KEY", "test-fernet-key")
    monkeypatch.setenv("SMART_ROUTER_KEY", "test-admin-key")
    monkeypatch.setenv("DB_REVISION_TOKEN", "x")
    monkeypatch.setenv("YAML_TOKEN", "y")

    import router as router_module

    async with LifespanManager(router_module.app):
        service = router_module.app.state.router
        manager = router_module.app.state.config_manager
        assert manager.active_revision_id == rev.id
        assert set(service.clients) == {"db-conn"}
        assert "db-route" in service.routes
        assert "yaml-route" not in service.routes
        assert "db-conn" in service.router_engine.snapshot.connections
        assert "db-route" in service.router_engine.snapshot.routes

        # Data-plane proof: the live SmartRouter must be able to serve requests
        # using the DB revision — correct base_url and a credential header built
        # from the revision's token_env (not the YAML bootstrap).
        cfg = service._upstream_config("db-conn")
        assert cfg["base_url"] == "https://db-revision.test"
        assert cfg["auth"]["token_env"] == "DB_REVISION_TOKEN"
        headers = service._upstream_headers("db-conn", {"accept": "application/json"})
        assert headers["authorization"] == "Bearer x"
        assert service.routes["db-route"]["candidates"][0].upstream == "db-conn"

        transport = httpx.ASGITransport(app=router_module.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            ready = await client.get("/health/ready")
            assert ready.status_code in {200, 503}
            body = ready.json()
            assert body["checks"]["runtime_snapshot"]["status"] == "ok"
            assert body["runtime_revision_id"] == rev.id
            assert body["checks"]["revision_convergence"] == {
                "status": "ok",
                "runtime_revision_id": rev.id,
                "db_revision_id": rev.id,
            }

            # Simulate control-plane/data-plane divergence: the live runtime is
            # pinned to a revision that is not the durable active one.  Readiness
            # must report the divergence and name both IDs (status body carries
            # it; the endpoint keeps the repo's 200+structured-body contract).
            manager._active_revision_id = "rev_stale_000000000"
            diverged = await client.get("/health/ready")
            dbody = diverged.json()
            assert dbody["status"] == "unavailable"
            assert dbody["checks"]["revision_convergence"]["status"] == "unavailable"
            assert dbody["checks"]["revision_convergence"]["db_revision_id"] == rev.id
            assert dbody["runtime_revision_id"] == "rev_stale_000000000"
            manager._active_revision_id = rev.id  # restore for activation slice

        # ── activation without restart (real admin HTTP endpoint) ──────────
        # A fresh manager swap must re-point the LIVE SmartRouter + data-plane
        # helpers to the newly active revision, proving §20 "activation requires
        # no restart" and "invalid config cannot replace last-known-good".
        async with factory() as session:
            repo2 = RevisionRepository(session)
            rev2 = await repo2.create_draft(
                {
                    "connections": {
                        "db-conn-2": {
                            "connection_id": "db-conn-2",
                            "base_url": "https://db-revision-2.test",
                            "auth_mode": "x-api-key",
                            "token_env": "DB_REVISION2_TOKEN",
                        }
                    },
                    "routes": {
                        "db-route-2": {
                            "route_name": "db-route-2",
                            "strategy": "priority",
                            "candidates": [
                                {"upstream": "db-conn-2", "model": "m2", "weight": 1}
                            ],
                            "fallback": [],
                            "generated": False,
                        }
                    },
                }
            )
            await session.commit()
        monkeypatch.setenv("DB_REVISION2_TOKEN", "z")

        os.environ["SMART_ROUTER_KEY"] = "test-admin-key"
        admin_headers = {"Authorization": "Bearer test-admin-key"}
        activation_calls: list[str] = []
        original_activate = manager.activate

        async def record_activation(revision_id, session):
            activation_calls.append(revision_id)
            return await original_activate(revision_id, session)

        manager.activate = record_activation
        transport2 = httpx.ASGITransport(app=router_module.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport2, base_url="http://testserver", headers=admin_headers
        ) as admin_client:
            activate = await admin_client.post(f"/api/admin/v1/revisions/{rev2.id}/activate")
            assert activate.status_code == 200, activate.text
        assert activation_calls == [rev2.id]

        # Same live service object — no lifespan restart happened.
        assert router_module.app.state.router is service
        assert manager.active_revision_id == rev2.id
        assert set(service.clients) == {"db-conn-2"}
        assert "db-route-2" in service.routes
        assert "db-route" not in service.routes
        cfg2 = service._upstream_config("db-conn-2")
        assert cfg2["base_url"] == "https://db-revision-2.test"
        headers2 = service._upstream_headers("db-conn-2", {})
        assert headers2.get("x-api-key") == "z"

        # Invalid activation must NOT replace last-known-good.
        async with factory() as session:
            bad = await RevisionRepository(session).create_draft(
                {
                    "connections": {},
                    "routes": {
                        "broken": {
                            "route_name": "broken",
                            "strategy": "priority",
                            "candidates": [{"upstream": "ghost", "model": "x", "weight": 1}],
                            "fallback": [],
                            "generated": False,
                        }
                    },
                }
            )
            await session.commit()
        async with httpx.AsyncClient(
            transport=transport2, base_url="http://testserver", headers=admin_headers
        ) as admin_client:
            rejected = await admin_client.post(f"/api/admin/v1/revisions/{bad.id}/activate")
        assert rejected.status_code == 400, rejected.text
        assert manager.active_revision_id == rev2.id
        assert set(service.clients) == {"db-conn-2"}

    await engine.dispose()
