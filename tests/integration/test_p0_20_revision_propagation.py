"""Two live routers, independent Redis clients, shared SQLite authority."""
import asyncio

import httpx
import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.gateway.api.admin import router as admin_router
from apps.gateway.db.dependencies import get_session
from apps.gateway.db.models import Base
from apps.gateway.db.revisions import RevisionRepository
from apps.gateway.runtime.manager import RuntimeConfigManager, REVISION_ACTIVATION_CHANNEL, snapshot_to_legacy_config
from router import SmartRouter


def config(model):
    return {"connections": {"conn": {"base_url": "https://provider.test", "auth_mode": "bearer", "token_env": "TEST_TOKEN"}},
            "routes": {"route": {"strategy": "priority", "candidates": [{"upstream": "conn", "model": model}]}}}


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_admin_activation_and_rollback_propagate(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_KEY", "test-admin-key")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        repo = RevisionRepository(session)
        a = await repo.create_draft(config("model-a"))
        b = await repo.create_draft(config("model-b"))
        await repo.activate(a.id)
        await session.commit()
    server = FakeServer()
    clients = [FakeAsyncRedis(server=server, decode_responses=True) for _ in range(2)]
    managers = [RuntimeConfigManager(redis_client=c, session_factory=factory) for c in clients]
    services = []
    app = FastAPI()
    app.include_router(admin_router, prefix="/api/admin/v1")
    async def session_dep():
        async with factory() as session:
            yield session
    app.dependency_overrides[get_session] = session_dep
    try:
        for manager in managers:
            async with factory() as session:
                snapshot = await manager.load_initial(session)
            service = SmartRouter(snapshot_to_legacy_config(snapshot))
            await service.start()
            services.append(service)
            manager._router = service
            manager.start_listener()
        async with asyncio.timeout(3):
            while (await clients[0].pubsub_numsub(REVISION_ACTIVATION_CHANNEL))[0][1] != 2:
                await asyncio.sleep(0.01)
        app.state.config_manager = managers[0]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-admin-key"}) as client:
            for revision, action, model in [(b, "activate", "model-b"), (a, "rollback", "model-a")]:
                response = await client.post(f"/api/admin/v1/revisions/{revision.id}/{action}")
                assert response.status_code == 200, response.text
                await eventually(lambda: managers[1].active_revision_id == revision.id)
                assert services[1].routes["route"]["candidates"][0].model == model
            # Delayed/stale messages must not resurrect an inactive revision.
            await managers[1]._apply_broadcast(b.id)
            assert managers[1].active_revision_id == a.id
            assert services[1].routes["route"]["candidates"][0].model == "model-a"
    finally:
        for manager in managers:
            await manager.stop_listener()
            assert manager._listener_task is None
        for service in services:
            await service.close()
        for client in clients:
            await client.aclose()
        await engine.dispose()
