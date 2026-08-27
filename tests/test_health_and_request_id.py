import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router import app


@pytest.mark.asyncio
async def test_live_health_endpoint_returns_request_id():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers.get("x-request-id")


@pytest.mark.asyncio
async def test_request_id_header_is_preserved_when_client_sends_one():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health/live", headers={"X-Request-ID": "client-request-123"})

    assert response.status_code == 200
    assert response.headers.get("x-request-id") == "client-request-123"


@pytest.mark.asyncio
async def test_ready_health_endpoint_returns_request_id():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers.get("x-request-id")
