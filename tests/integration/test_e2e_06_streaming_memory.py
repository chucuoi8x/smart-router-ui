"""E2E-06 — Streaming memory: incremental parsing, no full-body accumulation.

SMART_ROUTER_P0_STABILIZATION_PLAN §E2E-06:

    Stream a large response from a mock provider.
    Verify memory usage does not grow approximately linearly with the entire
    response body for every concurrent stream.
    The router must use incremental parsing.

Three complementary layers, ordered by CI stability on Windows:

1. Structural: the provider streaming paths must not mirror every chunk into a
   single end-of-stream buffer, and the authoritative router stream path must
   cap the bytes it retains per stream.
2. Unit: ``IncrementalSSEUsageParser`` retains only an incomplete-line tail,
   measured directly (no allocator noise, fully deterministic).
3. Behavioural: ``tracemalloc`` peak over concurrent large mock streams, with a
   generous absolute bound far below full-body buffering.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import tracemalloc
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
import pytest

from apps.gateway.providers.cliproxy_bridge import CLIProxyBridgeDriver
from apps.gateway.providers.driver_context import DriverContext
from apps.gateway.providers.generic_anthropic import GenericAnthropicDriver
from apps.gateway.providers.generic_openai import GenericOpenAIDriver
from apps.gateway.providers.http_base import IncrementalSSEUsageParser
from router import Candidate, SmartRouter


# ── 1. structural assertions ──────────────────────────────────────────────────


def _source_of(obj: Any, method: str) -> str:
    try:
        return inspect.getsource(getattr(obj, method))
    except (OSError, TypeError):
        return ""


@pytest.mark.parametrize(
    "cls",
    [GenericOpenAIDriver, GenericAnthropicDriver, CLIProxyBridgeDriver],
    ids=lambda c: c.__name__,
)
def test_execute_stream_does_not_aggregate_full_response_body(cls: type) -> None:
    """Provider success path must not mirror every chunk into a full-body buffer."""
    src = _source_of(cls, "execute_stream")
    assert src, f"{cls.__name__}.execute_stream source is not inspectable"
    accumulates = bool(re.search(r"\b\w*(accumulated|sse_buf|sse_tail|buffer)\w*\.extend\s*\(\s*chunk\s*\)", src))
    if accumulates:
        assert re.search(r"del\s+\w*(accumulated|sse_buf|sse_tail|buffer)\w*\[", src), (
            f"{cls.__name__}.execute_stream buffers every chunk into one bytearray "
            "without trimming it: memory grows linearly with the response body per "
            "concurrent stream, violating E2E-06"
        )
    else:
        assert not re.search(r"bytes\s*\(\s*\w*(accumulated|sse_buf|buffer)\w*\s*\)", src), (
            f"{cls.__name__}.execute_stream parses an end-of-stream full-body buffer"
        )


def test_router_streaming_path_caps_retained_bytes() -> None:
    """The authoritative router stream path must bound its retained SSE tail."""
    src = _source_of(SmartRouter, "_stream_messages")
    assert src, "SmartRouter._stream_messages source is not inspectable"
    assert "_sse_tail_limit" in src, (
        "router keeps no tail cap: the SSE buffer grows with the whole response body"
    )
    tail_start = src.index("def _retain_sse_tail")
    tail_end = src.index("async def iterator", tail_start)
    tail_helper = src[tail_start:tail_end]
    assert "_sse_buf.extend(chunk)" in tail_helper
    assert "del _sse_buf[:-_sse_tail_limit]" in tail_helper


# ── 2. unit: parser retains only an incomplete-line tail ──────────────────────


def test_incremental_usage_parser_tail_stays_bounded() -> None:
    """Feeding a huge SSE stream one byte at a time never grows the tail beyond
    the longest single SSE line, while usage is still merged correctly."""
    driver = GenericOpenAIDriver()
    parser = IncrementalSSEUsageParser(driver.parse_usage, driver._merge_stream_usage)
    filler = b"x" * 4096
    body = b"".join(
        [b'data: {"delta":{"text":"' + filler + b'"}}\n\n' for _ in range(2000)]
    ) + b'data: {"usage":{"prompt_tokens":11,"completion_tokens":22,"total_tokens":33}}\n\n'

    observed_max_tail = 0
    for byte in body:
        parser.feed(bytes([byte]))
        observed_max_tail = max(observed_max_tail, len(parser._tail))
    usage = parser.finish()

    assert usage["input_tokens"] == 11
    assert usage["output_tokens"] == 22
    assert usage["total_tokens"] == 33
    # Tail only ever holds the partially-received line, never the whole body.
    assert observed_max_tail < 2 * (len(filler) + 40), (
        f"parser tail peaked at {observed_max_tail} bytes for a "
        f"{len(body)}-byte stream: parsing is not incremental"
    )
    assert parser._tail == bytearray()


def test_incremental_usage_parser_survives_malformed_events() -> None:
    """Malformed and non-JSON data lines are forwarded-irrelevant and skipped."""
    driver = GenericOpenAIDriver()
    parser = IncrementalSSEUsageParser(driver.parse_usage, driver._merge_stream_usage)
    parser.feed(b'data: not-json\n\n')
    parser.feed(b'event: ping\n')
    parser.feed(b'data: [DONE]\n\n')
    parser.feed(b'data: {"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n')
    assert parser.finish() == {
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
        "source": "provider_api",
        "confidence": "exact",
    }


# ── 3. behavioural: deterministic large mock streams + tracemalloc ────────────


class _AsyncMockTransport(httpx.AsyncBaseTransport):
    """Serve a prebuilt SSE body as a real streaming response."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=self._body,
        )


def _driver_ctx() -> DriverContext:
    return DriverContext(connection_id="c1", base_url="https://mock.test/v1", credential={"api_key": "k"})


class _Req:
    endpoint = "chat/completions"
    body = b'{"model":"gpt-4o","stream":true}'
    headers: dict[str, str] = {}
    method = "POST"


@pytest.mark.asyncio
async def test_provider_stream_reports_usage_without_full_body_buffer() -> None:
    """200 content events + a trailing usage event: byte-exact passthrough and
    correct usage, driven through the real httpx streaming API."""
    content_chunk = b'data: {"delta":{"text":"hello"}}\n\n'
    usage_chunk = (
        b'data: {"usage":{"prompt_tokens":5,"completion_tokens":7,"total_tokens":12}}\n\n'
        b"data: [DONE]\n\n"
    )
    body = content_chunk * 200 + usage_chunk
    client = httpx.AsyncClient(transport=_AsyncMockTransport(body))
    driver = GenericOpenAIDriver(client=client)
    try:
        total = 0
        async for chunk in driver.execute_stream(_driver_ctx(), _Req()):
            total += len(chunk)
    finally:
        await client.aclose()

    assert total == len(body)
    assert driver.last_stream_usage["input_tokens"] == 5
    assert driver.last_stream_usage["output_tokens"] == 7


class _ContextManager:
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _DeterministicMockProvider:
    """Yield a large SSE stream of unique chunks without materialising the body."""

    def __init__(self, num_chunks: int, chunk_size: int) -> None:
        self.num_chunks = num_chunks
        self.chunk_size = chunk_size
        self.bytes_yielded = 0

    async def chunks(self) -> AsyncIterator[bytes]:
        for index in range(self.num_chunks):
            prefix = f'data: {{"delta":{{"text":"{index:06d}:'.encode()
            suffix = b'"}}\n\n'
            chunk = prefix + b"x" * max(0, self.chunk_size - len(prefix) - len(suffix)) + suffix
            self.bytes_yielded += len(chunk)
            yield chunk
        usage = b'data: {"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}\n\n'
        self.bytes_yielded += len(usage)
        yield usage


@pytest.mark.asyncio
async def test_concurrent_large_router_streams_keep_peak_memory_bounded(monkeypatch) -> None:
    """Three concurrent ~2 MB mock streams must not retain their bodies.

    Full-body buffering would hold ~6 MB per request set; the bounded tail
    holds 64 KB per stream. The bound is a loose absolute so Windows CI
    allocator noise cannot make it flaky.
    """
    num_streams = 3
    num_chunks = 2048
    chunk_size = 1024
    body_size = num_chunks * chunk_size

    router = SmartRouter(
        {"routes": {}, "upstreams": {}, "logging": {"level": "CRITICAL"}}
    )
    candidate = Candidate("mock", "model")
    providers: list[_DeterministicMockProvider] = []

    async def fake_open_stream(cand: Candidate, body: Any, headers: Any, path: str):
        provider = _DeterministicMockProvider(num_chunks, chunk_size)
        providers.append(provider)
        iterator = provider.chunks()
        first = await anext(iterator)
        return SimpleNamespace(
            context_manager=_ContextManager(),
            response=SimpleNamespace(
                status_code=200, headers={"content-type": "text/event-stream"}
            ),
            iterator=iterator,
            first_chunk=first,
            candidate=cand,
        )

    monkeypatch.setattr(router, "_open_stream", fake_open_stream)

    async def consume_one(index: int) -> int:
        response = await router._stream_messages(
            {"model": "large", "stream": True},
            {},
            [candidate],
            "large",
            "/v1/messages",
            request_id=f"req-{index}",
        )
        total = 0
        async for chunk in response.body_iterator:
            total += len(chunk)
        return total

    tracemalloc.start()
    try:
        totals = await asyncio.gather(*(consume_one(i) for i in range(num_streams)))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(providers) == num_streams
    assert all(total >= body_size * 0.95 for total in totals), totals
    assert all(p.bytes_yielded >= body_size * 0.95 for p in providers)

    buffered_bodies = body_size * num_streams  # ~6 MB if buffering were full
    peak_bound = 1536 * 1024  # 1.5 MB — generous, yet 4x under full buffering
    assert peak < peak_bound, (
        f"peak {peak / 1024:.1f} KB exceeds {peak_bound / 1024:.1f} KB; "
        f"full-body buffering would need ~{buffered_bodies / 1024:.1f} KB"
    )
