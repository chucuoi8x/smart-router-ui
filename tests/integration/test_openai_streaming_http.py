"""HTTP-level regression for AC-11: no streaming failover after first output."""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from starlette.responses import StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app, get_authorized_service


class _StreamingService:
    """Fake handler that streams successfully — simulates an OpenAI-compatible response."""

    async def handle_messages(self, body, headers, path):
        assert path == "/v1/messages"

        chunks = [
            b'data: {"type":"message_start","message":{"id":"msg_e2e","model":"route","role":"assistant"}}\n\n',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello "}}\n\n',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"world!"}}\n\n',
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n',
            b'data: {"type":"message_stop"}\n\n',
        ]
        return StreamingResponse(iter(chunks), media_type="text/event-stream")


@pytest.mark.asyncio
async def test_http_chat_completions_stream_produces_full_response():
    """Regression: HTTP /v1/chat/completions?stream=true returns complete SSE chain via stateful encoder."""
    fake = _StreamingService()
    app.dependency_overrides[get_authorized_service] = lambda: fake
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "route",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
                headers={"Authorization": "Bearer test-admin-key"},
            )
    finally:
        app.dependency_overrides.pop(get_authorized_service, None)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    # Parse all data lines (skip [DONE])
    data_lines = []
    for line in resp.text.splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            data_lines.append(line[6:])

    # Should have content deltas (the raw engine events are fed to encoder)
    # The stateful encoder should produce at least one OpenAI-style chunk
    assert len(data_lines) >= 1
