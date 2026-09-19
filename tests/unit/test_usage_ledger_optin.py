"""RED tests Step 158 — usage ledger opt-in safety."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_env_example_documents_usage_ledger_enabled_by_default():
    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    assert "USAGE_LEDGER_DB_ENABLED=true" in text


def test_usage_ledger_is_enabled_by_default_in_production_contract():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "router.py").read_text(encoding="utf-8")
    assert 'os.getenv("USAGE_LEDGER_DB_ENABLED", "true")' in text


@pytest.mark.asyncio
async def test_usage_ledger_repo_is_none_when_opt_in_disabled(monkeypatch):
    monkeypatch.delenv("USAGE_LEDGER_DB_ENABLED", raising=False)
    monkeypatch.setenv("USAGE_LEDGER_DB_ENABLED", "false")
    from router import get_optional_usage_ledger_repo

    gen = get_optional_usage_ledger_repo()
    val = await gen.__anext__()
    assert val is None
    # generator should be done after yielding None
    try:
        await gen.__anext__()
        assert False, "should have stopped"
    except StopAsyncIteration:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_usage_ledger_repo_construction_is_lazy_when_db_unavailable(monkeypatch):
    monkeypatch.setenv("USAGE_LEDGER_DB_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@127.0.0.1:1/db")
    from router import get_optional_usage_ledger_repo

    gen = get_optional_usage_ledger_repo()
    val = await gen.__anext__()
    # SQLAlchemy async sessions connect lazily; dependency construction must
    # not perform network I/O or raise. Request write path handles DB failure.
    assert val is not None
    await gen.aclose()
