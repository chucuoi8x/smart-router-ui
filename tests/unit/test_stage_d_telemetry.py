"""P0-09 — async Usage Ledger: non-blocking persistence queue.

P0-10 (incremental SSE UsageTap) is owned by sibling branch p0e/e2e-06 and is
deliberately not covered here.
"""
import asyncio

import pytest

from apps.gateway.usage.ledger import (
    AsyncUsageLedger,
    RequestRecord,
    UsageEvent,
    UsageLedgerRepository,
)


@pytest.mark.asyncio
async def test_async_usage_ledger_enqueues_without_waiting_for_database():
    persisted = []
    gate = asyncio.Event()

    async def persist(event):
        await gate.wait()
        persisted.append(event)

    ledger = AsyncUsageLedger(persist, maxsize=2)
    event = object()
    await ledger.emit(event)
    assert persisted == []
    assert ledger.pending == 1

    gate.set()
    await ledger.drain()
    assert persisted == [event]
    assert ledger.pending == 0


@pytest.mark.asyncio
async def test_async_usage_ledger_buffers_multiple_events():
    persisted = []

    async def persist(event):
        await asyncio.sleep(0)
        persisted.append(event)

    ledger = AsyncUsageLedger(persist, maxsize=8)
    for i in range(5):
        await ledger.emit(i)
    await ledger.drain()
    assert persisted == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_repository_record_usage_async_defers_db_write():
    """record_usage_async must not touch the session synchronously on emit."""

    class TrackingSession:
        def __init__(self):
            self.added = []
            self.flush_calls = 0

        def add(self, row):
            self.added.append(row)

        async def flush(self):
            self.flush_calls += 1

        async def commit(self):
            pass

    session = TrackingSession()
    repo = UsageLedgerRepository(session)
    event = UsageEvent.from_parsed_usage(
        request_id="req_1",
        attempt_id="att_1",
        provider_connection_id="conn_1",
        credential_id=None,
        model_resource_id="model_1",
        usage={"input_tokens": 3, "output_tokens": 4, "source": "provider_api", "confidence": "exact"},
    )

    await repo.record_usage_async(event)
    # Emitted onto the queue; persistence is scheduled, not inline-complete yet.
    queue = repo._async_ledger
    await queue.drain()

    assert session.flush_calls == 1
    assert len(session.added) == 1
    row = session.added[0]
    assert row.request_id == "req_1"
    assert row.total_tokens == 7
