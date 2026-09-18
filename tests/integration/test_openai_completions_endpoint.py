"""HTTP regression for OpenAI chat completions non-streaming path."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from starlette.responses import JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app, get_authorized_service


class _FakeService:
    async def handle_messages(self, body, headers, path):
        assert path == "/v1/messages"
        return JSONResponse(content={
            "id": "msg_resp",
            "model": "route",
            "role": "assistant",
            "content": [{"type": "text", "text": "hello world"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })


@pytest.mark.asyncio
async def test_chat_completions_non_stream_returns_openai_format():
    fake = _FakeService()
    app.dependency_overrides[get_authorized_service] = lambda: fake
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "route",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
                headers={"Authorization": "Bearer test-admin-key"},
            )
    finally:
        app.dependency_overrides.pop(get_authorized_service, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert len(data["choices"]) == 1
    choice = data["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "hello world"
    assert choice["finish_reason"] == "stop"
    assert data["usage"]["prompt_tokens"] == 10
    assert data["usage"]["completion_tokens"] == 5
    assert data["usage"]["total_tokens"] == 15
    assert data["model"] == "route"
