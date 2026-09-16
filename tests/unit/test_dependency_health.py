"""RED tests Step 157 — dependency readiness status."""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app


@pytest.mark.asyncio
async def test_ready_health_has_structured_database_and_redis_checks():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    checks = response.json()["checks"]
    for name in ("database", "redis"):
        assert checks[name]["status"] in {"ok", "degraded", "unavailable", "unknown"}
        assert "configured" in checks[name]
        assert "detail" not in checks[name] or isinstance(checks[name]["detail"], str)


def test_dependency_health_payload_has_no_connection_urls():
    from router import _dependency_health_payload

    payload = _dependency_health_payload(database_url="postgresql+asyncpg://user:secret@db/sr", redis_url="redis://:secret@redis:6379/0")
    text = str(payload)
    assert "secret" not in text
    assert "postgresql" not in text
    assert "redis://" not in text
    assert payload["database"]["configured"] is True
    assert payload["redis"]["configured"] is True
