"""Worker process entrypoint: catalog sync + usage aggregation.

Runs a lightweight polling loop against configured collectors and the
durable ledger.  Designed to be started as its own service in
docker-compose; exits cleanly on SIGTERM.
"""
from __future__ import annotations

import inspect
import logging
import os
import signal
import sys
import time
from typing import Any


# ── P0-15 Worker baseline responsibilities ──────────────────────────────

class UsagePersistenceWorker:
    """Persist usage events from async queue without blocking request path."""

    def __init__(self, persist_fn) -> None:
        self._persist_fn = persist_fn

    async def process_event(self, event) -> None:
        """Persist a single usage event."""
        result = self._persist_fn(event)
        if inspect.isawaitable(result):
            await result


class QuotaSyncWorker:
    """Synchronize quota resources from source of truth to runtime index."""

    def __init__(self, source, target) -> None:
        self._source = source
        self._target = target

    async def sync_once(self) -> None:
        """Pull resources from source and publish to target."""
        resources = await self._source.list_resources()
        result = self._target.replace_all(resources)
        if inspect.isawaitable(result):
            await result


class StaleReservationCleanupWorker:
    """Release reservations older than TTL to prevent capacity leaks.

    Note: Redis backend cannot list reservations without SCAN (violates
    P0-08/PR-09). This worker receives reservation entries from an external
    tracker (e.g. request ledger or an in-memory reservation registry) rather
    than querying the quota backend directly.
    """

    def __init__(self, backend, reservation_entries_provider, ttl_seconds: int = 3600) -> None:
        self._backend = backend
        self._reservation_entries = reservation_entries_provider
        self._ttl_seconds = ttl_seconds

    async def cleanup_once(self) -> int:
        """Release pending reservations past TTL; returns released count."""
        now = time.monotonic()
        released = 0
        for entry in self._reservation_entries():
            reservation_id = entry.get("reservation_id") or entry.get("id")
            if not reservation_id or entry.get("status", "pending") != "pending":
                continue
            if now - entry.get("created_at", now) <= self._ttl_seconds:
                continue
            try:
                result = self._backend.release(reservation_id)
                if inspect.isawaitable(result):
                    await result
                released += 1
            except Exception:
                # Already released/expired or backend hiccup: keep scanning.
                continue
        return released


logger = logging.getLogger("smart-router.worker")
_stop = False


def _handle_stop(signum: int, _frame: Any) -> None:
    global _stop
    logger.info("worker received signal %s — shutting down", signum)
    _stop = True


def _sync_catalogs_once() -> int:
    """Trigger catalog sync cho collectors đã cấu hình. Trả về số record."""
    try:
        from apps.worker.collectors import aibox_catalog as _catalog

        sync = getattr(_catalog, "sync_catalog", None)
        if sync is None:
            logger.debug("aibox_catalog.sync_catalog chưa có — bỏ qua")
            return 0
        return int(sync() or 0)
    except ImportError as exc:
        logger.debug("aibox_catalog không import được — bỏ qua: %s", exc)
        return 0
    except Exception as exc:
        logger.debug("aibox catalog sync không khả dụng: %s", exc)
        return 0


def main() -> int:
    logging.basicConfig(
        level=os.getenv("SMART_ROUTER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    interval = float(os.getenv("WORKER_POLL_SECONDS", "60"))
    logger.info("worker started — polling every %ss", interval)
    while not _stop:
        try:
            _sync_catalogs_once()
        except Exception:
            logger.exception("worker cycle failed — continuing")
        # sleep with graceful shutdown poll
        waited = 0.0
        while not _stop and waited < interval:
            time.sleep(min(1.0, interval - waited))
            waited += 1.0
    logger.info("worker stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
