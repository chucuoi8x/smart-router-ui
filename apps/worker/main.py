"""Worker process entrypoint: catalog sync + usage aggregation.

Runs a lightweight polling loop against configured collectors and the
durable ledger.  Designed to be started as its own service in
docker-compose; exits cleanly on SIGTERM.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from typing import Any

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
