"""Test harness: isolate the provider registry and keep unit tests offline.

Two independent concerns:

1. Registry persistence — see ``registry_db_url`` below.  SQLite proves
   repository/HTTP persistence behavior only.  It does NOT prove PostgreSQL
   dialect/behavior.  The PostgreSQL restart-persistence integration lives in
   ``tests/integration/test_persistent_provider_registry.py`` and runs only when
   ``SMART_ROUTER_TEST_DATABASE_URL`` points at an isolated PostgreSQL cluster;
   it fails loudly (never silently skips) when the URL is set but the cluster is
   unreachable.

2. Network isolation — since P0-03 the generic drivers perform real HTTP.
   Unit/admin tests that exercise control-plane wiring (health probe,
   deactivate, test-connection) must not depend on internet access, so an
   autouse fixture swaps the single driver-owned client construction point
   (``apps.gateway.providers.http_base.default_client``) for an
   ``httpx.MockTransport`` that answers discovery probes locally.  Tests that
   assert wire-level driver behavior (``test_provider_drivers.py``) inject
   their own recorder transport and are unaffected.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

import apps.gateway.db.session as db_session


@pytest.fixture(autouse=True)
def registry_db_url() -> Iterator[str]:
    """Fresh on-disk SQLite DB + real async engine bound to DATABASE_URL.

    Not a mock: a real file-backed SQLAlchemy async engine whose schema is
    created through the ORM metadata. Reset before and after so the process
    never leaks an engine or rows between tests.
    """
    import asyncio

    fd, path = tempfile.mkstemp(prefix="sr_registry_test_", suffix=".sqlite")
    os.close(fd)
    url = f"sqlite+aiosqlite:///{path}"
    os.environ["DATABASE_URL"] = url
    os.environ["SMART_ROUTER_ENV"] = "development"

    from apps.gateway.db.models import Base

    async def _setup() -> None:
        await db_session.dispose_engine()
        engine = db_session.init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup())
    try:
        yield url
    finally:
        asyncio.run(db_session.dispose_engine())
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("SMART_ROUTER_ENV", None)
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def clear_registry_caches() -> None:
    """Health-probe results are a process cache, not the source of truth.
    Clear them so a probe from one test cannot answer another test's query.
    Registry rows themselves live in the per-test SQLite database.
    """
    from apps.gateway.api import admin

    admin._provider_health_cache.clear()


@pytest.fixture(autouse=True)
def offline_driver_http(monkeypatch) -> None:
    """Route every driver-owned HTTP client through a local MockTransport."""
    from apps.gateway.providers import http_base

    def _responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "models": []})

    def _fake_default_client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_responder), timeout=timeout)

    monkeypatch.setattr(http_base, "default_client", _fake_default_client)
