"""Tests for DB-backed quota repository hydration at FastAPI startup."""

from __future__ import annotations

import pytest


class TestHydrateQuotaFromDb:
    """Verify SmartRouter._hydrate_quota_from_db() loads resources into the backend."""

    def _make_router(self, quota_backend=None):
        """Build a minimal SmartRouter with optional quota backend."""
        from router import SmartRouter

        config = {
            "routes": {"test-route": {"strategy": "priority", "candidates": []}},
            "upstreams": {},
            "logging": {"level": "CRITICAL"},
        }
        return SmartRouter(config, quota_reservations=quota_backend)

    @pytest.mark.asyncio
    async def test_graceful_when_no_db_engine(self):
        """Should skip silently when get_async_session_factory raises."""
        from apps.gateway.quota.reservations import InMemoryQuotaReservations
        from apps.gateway.quota.adapter import AsyncQuotaFacade

        raw = InMemoryQuotaReservations()
        router = self._make_router(AsyncQuotaFacade(raw))

        # Import the modules that would fail without a real DB
        import sys
        fake_session_mod = type(sys)("apps.gateway.db.session")
        fake_session_mod.get_async_session_factory = lambda: (_ for _ in ()).throw(
            RuntimeError("no engine")
        )
        saved = sys.modules.get("apps.gateway.db.session")
        sys.modules["apps.gateway.db.session"] = fake_session_mod

        try:
            # Should not raise — logs warning and returns
            await router._hydrate_quota_from_db()
        finally:
            if saved is not None:
                sys.modules["apps.gateway.db.session"] = saved
            else:
                sys.modules.pop("apps.gateway.db.session", None)

    @pytest.mark.asyncio
    async def test_skips_when_no_quota_backend(self):
        """Hydration is no-op when router has no quota_reservations."""
        router = self._make_router(None)
        await router._hydrate_quota_from_db()  # should not raise
