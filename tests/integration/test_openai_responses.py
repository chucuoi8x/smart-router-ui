"""HTTP regression: OpenAI Responses API compatibility boundary."""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from starlette.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app, get_authorized_service  # noqa: E402

AUTH = {"Authorization": "Bearer test-admin-key"}


class _FakeService:
    def __init__(self):
        self.body = None
        self.path = None

    async def handle_messages(self, body, headers, path):
        self.body = body
        self.path = path
        return JSONResponse(content={
            "id": "msg_response_1",
            "model": body["model"],
            "role": "assistant",
            "content": [{"type": "text", "text": "hello world"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })


async def _post(fake, payload):
    app.dependency_overrides[get_authorized_service] = lambda: fake
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/v1/responses", json=payload, headers=AUTH)
    finally:
        app.dependency_overrides.pop(get_authorized_service, None)


@pytest.mark.asyncio
async def test_responses_translates_string_input_and_returns_openai_envelope():
    fake = _FakeService()
    resp = await _post(fake, {"model": "route", "input": "hi"})

    assert resp.status_code == 200
    assert fake.path == "/v1/responses"
    assert fake.body["model"] == "route"
    assert fake.body["messages"] == [{"role": "user", "content": "hi"}]
    data = resp.json()
    assert data["object"] == "response"
    assert data["model"] == "route"
    assert data["output"][0]["role"] == "assistant"
    assert data["output"][0]["content"][0]["text"] == "hello world"
    assert data["usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


@pytest.mark.asyncio
async def test_responses_translates_input_message_list():
    fake = _FakeService()
    resp = await _post(fake, {
        "model": "route",
        "input": ["hi", {"role": "developer", "content": "be concise"}],
    })

    assert resp.status_code == 200
    assert fake.body["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "developer", "content": "be concise"},
    ]


@pytest.mark.asyncio
async def test_responses_invalid_json_returns_400():
    fake = _FakeService()
    app.dependency_overrides[get_authorized_service] = lambda: fake
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/v1/responses", content=b"not-json", headers={**AUTH, "Content-Type": "application/json"})
    finally:
        app.dependency_overrides.pop(get_authorized_service, None)
    assert resp.status_code == 400
