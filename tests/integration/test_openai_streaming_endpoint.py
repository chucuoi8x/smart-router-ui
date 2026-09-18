"""HTTP-level regression coverage for OpenAI chat streaming translation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from starlette.responses import StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app, get_authorized_service


class _FakeService:
    async def handle_messages(self, body, headers, path):
        assert path == "/v1/messages"
        assert body["model"] == "route"
        assert body["messages"] == [{"role": "user", "content": "hello"}]

        async def chunks():
            events = [
                {
                    "type": "message_start",
                    "message": {"id": "msg_1", "model": "route", "role": "assistant"},
                },
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "hi"},
                },
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            ]
            for event in events:
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")


@pytest.mark.asyncio
async def test_chat_completions_streaming_translates_sse_through_http_layer():
    fake = _FakeService()
    app.dependency_overrides[get_authorized_service] = lambda: fake
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "route",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
                headers={"Authorization": "Bearer test-admin-key"},
            )
    finally:
        app.dependency_overrides.pop(get_authorized_service, None)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"role":"assistant"' in response.text
    assert '"content":"hi"' in response.text
    assert '"finish_reason":"stop"' in response.text
    assert response.text.count("data: [DONE]") == 1
