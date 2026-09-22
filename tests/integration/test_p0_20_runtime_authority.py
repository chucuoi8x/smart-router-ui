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

    await engine.dispose()
