"""Integration test skeleton for proving runtime authority.

Drives the real FastAPI lifespan against a SQLite DB with an active revision
and a conflicting config.yaml.

Usage:
1. Copy to tests/integration/test_p0_20_runtime_authority.py.
2. Adapt the DB revision and YAML bootstrap to your target component.
3. Assert that the live SmartRouter clients/routes/engine snapshot originate from
   the DB revision, NOT the bootstrap YAML.
"""
from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from apps.gateway.db.models import Base
from apps.gateway.db.revisions import RevisionRepository


@pytest.mark.asyncio
async def test_lifespan_loads_active_db_revision_into_live_router(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime_authority.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # 1. Seed DB revision
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        repo = RevisionRepository(session)
        rev = await repo.create_draft({
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
                    "candidates": [{"upstream": "db-conn", "model": "db-model", "weight": 1}],
                    "fallback": [],
                    "generated": False,
                }
            },
        })
        await repo.activate(rev.id)
        await session.commit()

    # 2. Bootstrap YAML
    bootstrap = tmp_path / "config.yaml"
    bootstrap.write_text("upstreams:\n  yaml-conn:\n    base_url: https://yaml.test", encoding="utf-8")

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SMART_ROUTER_CONFIG", str(bootstrap))
    monkeypatch.setenv("DB_REVISION_TOKEN", "x")

    import router as router_module
    async with LifespanManager(router_module.app):
        service = router_module.app.state.router
        manager = router_module.app.state.config_manager
        
        # 3. Assert authority
        assert manager.active_revision_id == rev.id
        assert "db-conn" in service.clients
        assert "db-route" in service.routes
        assert "yaml-conn" not in service.clients

    await engine.dispose()
