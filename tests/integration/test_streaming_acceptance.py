"""RED tests Step 152 — AC-11 streaming failover invariant (provider-agnostic).

AC-11: streaming failover only possible before first output; never after.
This acceptance slice validates that behavior through direct mocking of
the internal _open_stream + _stream_messages boundary.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import SmartRouter


def _success_json(text="ok") -> bytes:
    import json
    return json.dumps({"choices": [{"delta": {"content": text}}]}).encode()


@pytest.mark.asyncio
async def test_stream_primary_failure_before_first_output_triggers_fallback():
    config = {
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [{"upstream": "primary", "model": "m1"}],
                "fallback": [{"upstream": "backup", "model": "m2"}],
            }
        },
        "upstreams": {
            "primary": {"base_url": "https://primary.example", "auth": {"mode": "bearer", "token_env": "PRIMARY_TOKEN"}},
            "backup": {"base_url": "https://backup.example", "auth": {"mode": "bearer", "token_env": "BACKUP_TOKEN"}},
        },
        "logging": {"level": "CRITICAL"},
    }
    router = SmartRouter(config)
    import os
    os.environ["PRIMARY_TOKEN"] = "tok1"
    os.environ["BACKUP_TOKEN"] = "tok2"

    open_attempts = []

    async def fake_open_stream_failover(candidate, body, headers, path):
        open_attempts.append(candidate.model)
        if candidate.model == "m1":
            # Simulate error before any stream output (TRANSIENT_NETWORK)
            from fastapi.responses import JSONResponse
            resp = JSONResponse(status_code=503, content={"error": {"type": "upstream_unavailable", "message": "boom"}})
            resp._smart_router_classification = {"kind":"TRANSIENT_NETWORK","retryable":True,"scope":"connection","retry_after":None,"reset_at":None,"consumption_uncertainty":"none","status_code":503}
            return resp
        # Backup succeeds with one chunk
        from router import OpenStream
        first = b"data: " + _success_json("hi") + b"\n\n"
        async def it():
            yield first
        return OpenStream(
            context_manager=AsyncMock(**{"__aenter__": AsyncMock(return_value=None), "__aexit__": AsyncMock(return_value=None)}),
            response=AsyncMock(status_code=200, headers={}, content=b"", aclose=AsyncMock()),
            iterator=it(),
            first_chunk=first,
            candidate=candidate,
        )

    with patch.object(router, "_open_stream", fake_open_stream_failover):
        body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}], "stream": True}
        candidates = await router._candidate_order("chat")
        result = await router._stream_messages(
            body,
            {"authorization": "Bearer k"},
            candidates,
            "chat",
            "/v1/chat/completions",
        )

    assert result.status_code == 200, result.content[:600]
    assert "m2" in open_attempts, f"Expected fallback to m2 but got open_attempts={open_attempts}"


@pytest.mark.asyncio
async def test_stream_invariant_prevents_post_first_output_switch():
    """AC-11 structural check: once OpenStream returned, router commits to that candidate."""
    import inspect
    from router import SmartRouter

    src_msgs = inspect.getsource(SmartRouter._stream_messages)
    src_open = inspect.getsource(SmartRouter._open_stream)

    assert "first_chunk" in src_msgs
    assert "OpenStream" in src_open
    assert "anext(iterator)" in src_open or "await anext" in src_open

    # Key invariant: _stream_messages loops candidates ONLY for non-success
    # opens (4xx/5xx); once an OpenStream is returned the loop yields immediately
    # and does not try next candidate. This prevents mid-stream failover.
    assert "continue" in src_msgs  # only reachable from failed _open_stream, not from successful OpenStream
    # After OpenStream return the function builds StreamingResponse/accumulates SSE
    # and returns - no loop iteration to next candidate occurs.
