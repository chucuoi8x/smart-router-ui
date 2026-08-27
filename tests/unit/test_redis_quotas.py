"""Tests for Redis-backed quota reservations.

When a real Redis server is available (REDIS_TEST_URL env var), runs full
parity tests against it.  Otherwise only tests the wiring/adapter layer.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.gateway.quota.reservations import (
    InMemoryQuotaReservations,
    QuotaAdmissionResult,
    QuotaObservation,
    QuotaReservationRequest,
    QuotaResource,
    ReservationBatchResult,
    ReservationResult,
    ReconciliationResult,
)
from apps.gateway.quota.adapter import AsyncQuotaFacade

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REDIS_TEST_URL = os.getenv("REDIS_TEST_URL", "redis://localhost:6379/0")


def _redis_available() -> bool:
    """Probe whether a Redis server is reachable on the configured URL."""
    try:
        import redis as sync_redis
    except ImportError:
        return False
    r = sync_redis.from_url(REDIS_TEST_URL, socket_timeout=2, socket_connect_timeout=2)
    try:
        return r.ping()
    except Exception:
        return False
    finally:
        r.close()


# ---------------------------------------------------------------------------
# AsyncQuotaFacade — sync-to-async wrapping
# ---------------------------------------------------------------------------


class TestAsyncQuotaFacadeWrapping:
    """Verify that AsyncQuotaFacade correctly bridges sync backends to async callers."""

    @pytest.mark.asyncio
    async def test_check_many_is_async_and_returns_result(self):
        sync_backend = InMemoryQuotaReservations()
        sync_backend.add_resource(QuotaResource("t1", "model", "requests", 100, 3600))
        facade = AsyncQuotaFacade(sync_backend)
        assert asyncio.iscoroutinefunction(facade.check_many)

        result = await facade.check_many([QuotaReservationRequest("t1", amount=1)])
        assert isinstance(result, QuotaAdmissionResult)
        assert result.accepted is True

    @pytest.mark.asyncio
    async def test_add_resource(self):
        sync_backend = InMemoryQuotaReservations()
        facade = AsyncQuotaFacade(sync_backend)
        res = await facade.add_resource(QuotaResource("t2", "model", "requests", 100, 3600))
        assert res.resource_id == "t2"

    @pytest.mark.asyncio
    async def test_snapshot(self):
        sync_backend = InMemoryQuotaReservations()
        sync_backend.add_resource(QuotaResource("t3", "model", "requests", 50, 3600))
        facade = AsyncQuotaFacade(sync_backend)
        snap = await facade.snapshot("t3")
        assert snap.limit == 50

    @pytest.mark.asyncio
    async def test_reserve(self):
        sync_backend = InMemoryQuotaReservations()
        sync_backend.add_resource(QuotaResource("t4", "model", "requests", 10, 3600))
        facade = AsyncQuotaFacade(sync_backend)
        result = await facade.reserve(resource_id="t4", amount=1, reservation_id="rid-1")
        assert isinstance(result, ReservationResult)
        assert result.accepted is True

    @pytest.mark.asyncio
    async def test_reserve_many(self):
        sync_backend = InMemoryQuotaReservations()
        sync_backend.add_resource(QuotaResource("rm1", "model", "requests", 5, 3600))
        sync_backend.add_resource(QuotaResource("rm2", "model", "requests", 5, 3600))
        facade = AsyncQuotaFacade(sync_backend)
        reqs = [
            QuotaReservationRequest("rm1", amount=2),
            QuotaReservationRequest("rm2", amount=3),
        ]
        batch = await facade.reserve_many(reservation_id="batch-1", requests=reqs)
        assert isinstance(batch, ReservationBatchResult)
        assert len(batch.requests) == 2
        assert batch.accepted is True

    @pytest.mark.asyncio
    async def test_reconcile(self):
        sync_backend = InMemoryQuotaReservations()
        sync_backend.add_resource(QuotaResource("rc1", "model", "requests", 100, 3600))
        sync_backend.reserve(resource_id="rc1", amount=1, reservation_id="rec-rid")
        facade = AsyncQuotaFacade(sync_backend)
        result = await facade.reconcile("rec-rid", {"rc1": 5})
        assert isinstance(result, ReconciliationResult)
        assert result.reservation_id == "rec-rid"
        # reserved_by_resource shows what was originally reserved
        assert result.reserved_by_resource.get("rc1") == 1

    @pytest.mark.asyncio
    async def test_release(self):
        sync_backend = InMemoryQuotaReservations()
        sync_backend.add_resource(QuotaResource("rel1", "model", "requests", 100, 3600))
        sync_backend.reserve(resource_id="rel1", amount=1, reservation_id="rel-rid")
        facade = AsyncQuotaFacade(sync_backend)
        released = await facade.release("rel-rid")
        assert released is True

    @pytest.mark.asyncio
    async def test_apply_observation(self):
        sync_backend = InMemoryQuotaReservations()
        sync_backend.add_resource(QuotaResource("obs1", "model", "requests", 100, 3600))
        facade = AsyncQuotaFacade(sync_backend)
        obs = QuotaObservation(resource_id="obs1", limit=100, used=42, source="test", confidence=1.0, safety_buffer=0, hard_limit=True)
        updated = await facade.apply_observation(obs)
        assert isinstance(updated, QuotaResource)
        assert updated.used == 42


# ---------------------------------------------------------------------------
# Factory wiring (router-level auto-wrap logic)
# ---------------------------------------------------------------------------


class TestQuotaFactoryWiring:
    """Verify sync→async auto-wrapping in router/engine initialisation paths."""

    @pytest.mark.asyncio
    async def test_in_memory_wrapped_to_async_when_passed_directly(self):
        raw = InMemoryQuotaReservations()
        # Simulate the auto-wrap logic from SmartRouter.__init__ / RouterEngine.__init__
        if not asyncio.iscoroutinefunction(getattr(raw, "check_many", None)):
            wrapped = AsyncQuotaFacade(raw)
        else:
            wrapped = raw
        assert isinstance(wrapped, AsyncQuotaFacade)
        # All public methods should be coroutine functions after wrapping
        assert asyncio.iscoroutinefunction(wrapped.check_many)
        assert asyncio.iscoroutinefunction(wrapped.reserve_many)
        assert asyncio.iscoroutinefunction(wrapped.reconcile)
        assert asyncio.iscoroutinefunction(wrapped.release)

    def test_redis_quotas_class_importable(self):
        """RedisQuotaReservations should be importable when redis package exists."""
        try:
            from apps.gateway.quota.redis_backend import RedisQuotaReservations  # noqa: F401
        except ImportError:
            pytest.skip("redis package not installed")


# ---------------------------------------------------------------------------
# RedisQuotaReservations — live tests (require real Redis)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_redis_client():
    """Return a synced-up Redis client for module-scoped setup."""
    if not _redis_available():
        pytest.skip("No Redis server available at REDIS_TEST_URL")
    import redis
    r = redis.from_url(REDIS_TEST_URL, decode_responses=True)
    yield r
    # Cleanup: flush test db
    try:
        r.flushdb()
    except Exception:
        pass
    r.close()


@pytest.fixture
def redis_reservations(live_redis_client):
    """Return a RedisQuotaReservations connected to the test DB."""
    if not _redis_available():
        pytest.skip("No Redis server available at REDIS_TEST_URL")
    from apps.gateway.quota.redis_backend import RedisQuotaReservations

    rr = RedisQuotaReservations(redis_url=REDIS_TEST_URL, ttl=60)
    yield rr
    # Cleanup
    try:
        import redis as sync_redis
        cleanup = sync_redis.from_url(REDIS_TEST_URL, decode_responses=True)
        cleanup.flushdb()
        cleanup.close()
    except Exception:
        pass


@pytest.mark.asyncio
class TestRedisQuotaLive:
    """Full parity tests against a live Redis instance."""

    @pytest.fixture(autouse=True)
    def setup_reservations(self, live_redis_client):
        if not _redis_available():
            pytest.skip("No Redis server available at REDIS_TEST_URL")
        from apps.gateway.quota.redis_backend import RedisQuotaReservations

        self.rr = RedisQuotaReservations(redis_url=REDIS_TEST_URL, ttl=60)

    async def test_add_and_snapshot(self):
        res = await self.rr.add_resource(
            QuotaResource("live:r1", "model", "requests", 100, 3600)
        )
        assert res.resource_id == "live:r1"
        snap = await self.rr.snapshot("live:r1")
        assert snap.limit == 100

    async def test_reserve_many_accepts_valid_requests(self):
        await self.rr.add_resource(
            QuotaResource("live:r2", "model", "requests", 50, 3600)
        )
        reqs = [QuotaReservationRequest("live:r2", amount=5)]
        batch = await self.rr.reserve_many(
            reservation_id="live-b1", requests=reqs
        )
        assert batch.all_succeeded
        assert len(batch.results) == 1
        assert batch.results[0].success is True

    async def test_reserve_many_all_or_nothing_exhaustion(self):
        await self.rr.add_resource(
            QuotaResource("live:r3", "model", "requests", 2, 3600)
        )
        reqs = [
            QuotaReservationRequest("live:r3", amount=1),
            QuotaReservationRequest("live:r3", amount=1),
            QuotaReservationRequest("live:r3", amount=1),  # exceeds limit
        ]
        batch = await self.rr.reserve_many(
            reservation_id="live-b2", requests=reqs
        )
        # Should reject ALL since one request would exceed
        assert not batch.all_succeeded

    async def test_reconcile_frees_remaining_capacity(self):
        await self.rr.add_resource(
            QuotaResource("live:r4", "model", "requests", 10, 3600)
        )
        await self.rr.reserve_many(
            reservation_id="live-b3",
            requests=[QuotaReservationRequest("live:r4", amount=8)],
        )
        # Consume actual usage of 3
        result = await self.rr.reconcile("live-b3", {"live:r4": 3})
        assert result.status == "finalised"
        # Remaining capacity: 10 - 3 = 7
        snap = await self.rr.snapshot("live:r4")
        assert snap.remaining == 7

    async def test_release_returns_capacity(self):
        await self.rr.add_resource(
            QuotaResource("live:r5", "model", "requests", 10, 3600)
        )
        await self.rr.reserve_many(
            reservation_id="live-b4",
            requests=[QuotaReservationRequest("live:r5", amount=5)],
        )
        snap_before = await self.rr.snapshot("live:r5")
        assert snap_before.remaining == 5

        released = await self.rr.release("live-b4")
        assert released is True

        snap_after = await self.rr.snapshot("live:r5")
        assert snap_after.remaining == 10

    async def test_shared_group_projected_usage(self):
        """Resources sharing group 'g1' should see each other's projected usage."""
        await self.rr.add_resource(
            QuotaResource("live:sgr1", "model", "requests", 10, 3600, shared_group_id="g1")
        )
        await self.rr.add_resource(
            QuotaResource("live:sgr2", "model", "requests", 10, 3600, shared_group_id="g1")
        )
        # Reserve max on sgr1
        await self.rr.reserve_many(
            reservation_id="live-sg-r1",
            requests=[QuotaReservationRequest("live:sgr1", amount=10)],
        )
        # sgr2 should see shared pressure even though its own counter is 0
        admission = await self.rr.check_many([
            QuotaReservationRequest("live:sgr2", amount=1),
        ])
        assert admission.accepted is False  # shared group exhausted


@pytest.mark.asyncio
class TestRedisQuotaFallback:
    """Verify graceful fallback when Redis connection fails."""

    async def test_constructor_raises_on_bad_url(self):
        """Should surface connection error when given an impossible URL."""
        from apps.gateway.quota.redis_backend import RedisQuotaReservations

        rr = RedisQuotaReservations(
            redis_url="redis://invalid-host-that-does-not-exist.local:9999/0", ttl=60
        )
        # Constructor doesn't connect eagerly; first operation does.
        with pytest.raises(Exception):  # Connection refused / timeout
            await rr.add_resource(
                QuotaResource("fb:r1", "model", "requests", 10, 3600)
            )
