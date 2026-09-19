"""RED tests P0-15 — worker baseline responsibilities."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_usage_persistence_worker_consumes_async_ledger():
    """Worker must persist usage events from async queue without blocking request path."""
    from apps.worker.main import UsagePersistenceWorker
    from apps.gateway.usage.ledger import UsageEvent

    persisted = []
    async def mock_persist(event):
        persisted.append(event)

    worker = UsagePersistenceWorker(persist_fn=mock_persist)
    event = UsageEvent(
        request_id="req-1",
        attempt_id="att-1",
        provider_connection_id="conn-1",
        credential_id="cred-1",
        model_resource_id="model-1",
        input_tokens=10,
        output_tokens=20,
        total_tokens=30,
    )
    await worker.process_event(event)
    assert persisted == [event]


@pytest.mark.asyncio
async def test_quota_sync_worker_syncs_resources():
    """Worker must sync quota resources from source of truth."""
    from apps.worker.main import QuotaSyncWorker

    source = MagicMock()
    source.list_resources = AsyncMock(return_value=[
        {"resource_id": "model:a", "limit": 100},
        {"resource_id": "model:b", "limit": 200},
    ])
    target = MagicMock()
    target.replace_all = AsyncMock()

    worker = QuotaSyncWorker(source=source, target=target)
    await worker.sync_once()
    target.replace_all.assert_called_once()


@pytest.mark.asyncio
async def test_stale_reservation_cleanup_worker_releases_expired():
    """Worker must release pending reservations older than TTL from tracker entries."""
    from apps.worker.main import StaleReservationCleanupWorker
    import time

    now = time.monotonic()
    entries = [
        {"reservation_id": "rid-1", "created_at": now - 7200, "status": "pending"},  # 2h old
        {"reservation_id": "rid-2", "created_at": now - 60, "status": "pending"},     # 1m old
        {"reservation_id": "rid-3", "created_at": now - 7200, "status": "settled"},   # old but settled
    ]
    backend = MagicMock()
    backend.release = AsyncMock(return_value=True)

    worker = StaleReservationCleanupWorker(
        backend=backend,
        reservation_entries_provider=lambda: entries,
        ttl_seconds=3600,
    )
    released = await worker.cleanup_once()
    assert released == 1
    backend.release.assert_called_once_with("rid-1")


@pytest.mark.asyncio
async def test_cleanup_worker_survives_backend_errors():
    """A failing release must not abort the whole sweep."""
    from apps.worker.main import StaleReservationCleanupWorker
    import time

    now = time.monotonic()
    entries = [
        {"reservation_id": "bad", "created_at": now - 7200, "status": "pending"},
        {"reservation_id": "good", "created_at": now - 7200, "status": "pending"},
    ]

    async def flaky_release(rid):
        if rid == "bad":
            raise RuntimeError("gone")
        return True

    backend = MagicMock()
    backend.release = AsyncMock(side_effect=flaky_release)

    worker = StaleReservationCleanupWorker(
        backend=backend, reservation_entries_provider=lambda: entries, ttl_seconds=3600
    )
    assert await worker.cleanup_once() == 1
