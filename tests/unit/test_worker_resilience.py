"""RED tests Step 159 — worker collector resilience."""
import builtins
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.worker import main as worker


def test_worker_sync_returns_zero_when_collector_import_fails(monkeypatch):
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name.startswith("apps.worker.collectors"):
            raise ImportError("collector unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    assert worker._sync_catalogs_once() == 0


def test_worker_sync_returns_zero_when_sync_catalog_missing(monkeypatch):
    stub = types.ModuleType("apps.worker.collectors.aibox_catalog")
    package = types.ModuleType("apps.worker.collectors")
    package.aibox_catalog = stub
    monkeypatch.setitem(sys.modules, "apps.worker.collectors", package)
    monkeypatch.setitem(sys.modules, "apps.worker.collectors.aibox_catalog", stub)
    assert worker._sync_catalogs_once() == 0


def test_worker_sync_returns_zero_when_sync_catalog_raises(monkeypatch):
    def explode():
        raise RuntimeError("collector failed")

    stub = types.ModuleType("apps.worker.collectors.aibox_catalog")
    stub.sync_catalog = explode
    package = types.ModuleType("apps.worker.collectors")
    package.aibox_catalog = stub
    monkeypatch.setitem(sys.modules, "apps.worker.collectors", package)
    monkeypatch.setitem(sys.modules, "apps.worker.collectors.aibox_catalog", stub)
    assert worker._sync_catalogs_once() == 0


def test_worker_sync_returns_record_count_on_success(monkeypatch):
    stub = types.ModuleType("apps.worker.collectors.aibox_catalog")
    stub.sync_catalog = lambda: 12
    package = types.ModuleType("apps.worker.collectors")
    package.aibox_catalog = stub
    monkeypatch.setitem(sys.modules, "apps.worker.collectors", package)
    monkeypatch.setitem(sys.modules, "apps.worker.collectors.aibox_catalog", stub)
    assert worker._sync_catalogs_once() == 12
