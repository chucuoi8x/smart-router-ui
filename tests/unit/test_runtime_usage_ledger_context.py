"""Tests for runtime UsageLedgerRepository binding via request ContextVars."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class TestRuntimeUsageLedgerContext:
    def _make_router(self, fallback_ledger=None):
        from router import SmartRouter

        config = {
            "routes": {"test-route": {"strategy": "priority", "candidates": []}},
            "upstreams": {},
            "logging": {"level": "CRITICAL"},
        }
        return SmartRouter(config, usage_ledger=fallback_ledger)

    def test_active_usage_ledger_prefers_request_context(self):
        import router as router_mod

        fallback = MagicMock(name="fallback-ledger")
        request_repo = MagicMock(name="request-repo")
        service = self._make_router(fallback)

        token = router_mod._REQUEST_USAGE_LEDGER.set(request_repo)
        try:
            assert service._active_usage_ledger() is request_repo
        finally:
            router_mod._REQUEST_USAGE_LEDGER.reset(token)

        assert service._active_usage_ledger() is fallback

    def test_latest_usage_tokens_reads_request_event_cache(self):
        import router as router_mod

        service = self._make_router(fallback_ledger=None)
        event = SimpleNamespace(
            attempt_id="attempt-1",
            input_tokens=7,
            output_tokens=11,
            total_tokens=18,
        )

        token = router_mod._REQUEST_USAGE_EVENTS.set([event])
        try:
            assert service._get_latest_usage_tokens(attempt_id="attempt-1") == {
                "input_tokens": 7,
                "output_tokens": 11,
                "total_tokens": 18,
            }
        finally:
            router_mod._REQUEST_USAGE_EVENTS.reset(token)

    @pytest.mark.asyncio
    async def test_optional_usage_ledger_dependency_disabled_by_default(self, monkeypatch):
        from router import get_optional_usage_ledger_repo

        monkeypatch.delenv("USAGE_LEDGER_DB_ENABLED", raising=False)
        dependency = get_optional_usage_ledger_repo()
        value = await anext(dependency)
        assert value is None
        with pytest.raises(StopAsyncIteration):
            await anext(dependency)

    @pytest.mark.asyncio
    async def test_optional_usage_ledger_dependency_falls_back_when_db_unavailable(self, monkeypatch):
        from router import get_optional_usage_ledger_repo
        import apps.gateway.db.session as db_session

        monkeypatch.setenv("USAGE_LEDGER_DB_ENABLED", "true")
        monkeypatch.setattr(
            db_session,
            "get_async_session_factory",
            lambda: (_ for _ in ()).throw(RuntimeError("no engine")),
        )
        dependency = get_optional_usage_ledger_repo()
        value = await anext(dependency)
        assert value is None
        with pytest.raises(StopAsyncIteration):
            await anext(dependency)
