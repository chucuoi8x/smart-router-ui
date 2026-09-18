import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.openai_compat import openai_request_to_router, router_response_to_openai, router_stream_to_openai


class TestRequestTranslation:
    def test_simple_text_passthrough(self):
        raw = {"model": "test-route", "messages": [{"role": "user", "content": "Hi"}]}
        body = openai_request_to_router(raw)
        assert body["model"] == "test-route"
        assert body["messages"][0] == {"role": "user", "content": "Hi"}

    def test_max_completion_tokens_forwarded(self):
        raw = {
            "model": "main",
            "messages": [{"role": "user", "content": "X"}],
            "max_completion_tokens": 512,
        }
        body = openai_request_to_router(raw)
        assert body["max_tokens"] == 512

    def test_temperature_passthrough(self):
        raw = {"model": "m", "messages": [], "temperature": 0.7}
        body = openai_request_to_router(raw)
        assert body["temperature"] == 0.7

    def test_system_becomes_string_prompt(self):
        raw = {
            "model": "r",
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"},
            ],
        }
        body = openai_request_to_router(raw)
        assert body.get("system") == "You are helpful"
        assert len(body["messages"]) == 1
        assert body["messages"][0]["role"] == "user"

    def test_multiple_system_concatenated(self):
        raw = {
            "model": "r",
            "messages": [
                {"role": "system", "content": "A"},
                {"role": "system", "content": "B"},
                {"role": "user", "content": "C"},
            ],
        }
        body = openai_request_to_router(raw)
        assert body["system"] == "A\nB"

    def test_missing_messages_defaults_empty_user(self):
        raw = {"model": "r"}
        body = openai_request_to_router(raw)
        assert body["messages"] == [{"role": "user", "content": ""}]

    def test_tool_role_wrapped_as_content(self):
        raw = {
            "model": "r",
            "messages": [
                {"role": "tool", "content": "result", "tool_call_id": "c1"},
            ],
        }
        body = openai_request_to_router(raw)
        expected = {"role": "user", "content": json.dumps({"tool_call_id": "c1", "result": "result"})}
        assert body["messages"][0] == expected

    def test_tools_stripped_out(self):
        raw = {
            "model": "r",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
        }
        body = openai_request_to_router(raw)
        assert "tools" not in body

    def test_metadata_and_stop_preserved(self):
        raw = {
            "model": "r",
            "messages": [],
            "metadata": {"correlationId": "abc"},
            "stop": ["END"],
        }
        body = openai_request_to_router(raw)
        assert body["metadata"] == {"correlationId": "abc"}
        assert body["stop"] == ["END"]


class TestResponseTranslation:
    def test_completion_basic(self):
        payload = {
            "id": "msg_123",
            "model": "claude-sonnet-4-5-20250514",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello world!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        oa = router_response_to_openai(payload, "claude-sonnet-4-5-20250514")
        assert oa["object"] == "chat.completion"
        choices = oa["choices"]
        assert len(choices) == 1
        c = choices[0]
        assert c["message"]["content"] == "Hello world!"
        assert c["message"]["role"] == "assistant"
        assert c["finish_reason"] == "stop"

    def test_no_content_stops_gracefully(self):
        payload = {
            "id": "msg_99",
            "model": "aibox/qwen3.6-flash",
            "role": "assistant",
            "content": [],
            "stop_reason": "end_turn",
        }
        oa = router_response_to_openai(payload, "qwen")
        c = oa["choices"][0]
        assert c["message"].get("content", "") == ""

    def test_usage_translated(self):
        payload = {
            "id": "m",
            "role": "assistant",
            "content": [{"type": "text", "text": "x"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 15, "output_tokens": 42},
        }
        oa = router_response_to_openai(payload, "qwen")
        u = oa["usage"]
        assert u["prompt_tokens"] == 15
        assert u["completion_tokens"] == 42
        assert u["total_tokens"] == 57

    def test_length_finish_reason(self):
        payload = {
            "id": "m",
            "role": "assistant",
            "content": [{"type": "text", "text": "long reply"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        oa = router_response_to_openai(payload, "qwen")
        assert oa["choices"][0]["finish_reason"] == "length"

    def test_already_openai_passes_through(self):
        oa = router_response_to_openai({
            "object": "chat.completion",
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {},
        }, "ignored")
        assert oa["object"] == "chat.completion"
        assert oa["choices"][0]["message"]["content"] == "hi"


class TestStreamTransformer:
    def _make_event(self, **kw):
        base = {"type": "message_start", "message": {"id": "msg_x", "model": "qwen", "role": "assistant", "content": [], "stop_reason": None}}
        base.update(kw)
        return base

    def test_text_delta_yields_chunks(self):
        events = [
            self._make_event(),
            {"type": "ping"},
            {"type": "content_block_delta", "delta": {"type": "text", "text": "Helo"}, "content_block_index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ]
        sse_lines = list(router_stream_to_openai(events, "qwen"))
        text_lines = [l for l in sse_lines if '"content":"Helo"' in l]
        assert len(text_lines) >= 1
        # SSE format: must start with "data: "
        assert text_lines[0].startswith("data: ")

    def test_last_chunk_is_done(self):
        events = [self._make_event(), {"type": "message_stop"}]
        lines = list(router_stream_to_openai(events, "qwen"))
        assert any('[DONE]' in l for l in lines)

    def test_ping_filtered(self):
        events = [self._make_event(), {"type": "ping"}, {"type": "message_stop"}]
        lines = list(router_stream_to_openai(events, "qwen"))
        ping_count = sum(1 for l in lines if '"type":"ping"' in l)
        assert ping_count == 0

    def test_incremental_encoder_emits_done_only_on_finish(self):
        from apps.gateway.openai_compat import OpenAIStreamEncoder

        encoder = OpenAIStreamEncoder("qwen")
        first = encoder.feed(self._make_event())
        assert any('"role":"assistant"' in line for line in first)
        delta = encoder.feed({"type": "content_block_delta", "delta": {"text": "Hi"}})
        assert any('"content":"Hi"' in line for line in delta)
        assert not any("[DONE]" in line for line in first + delta)
        assert encoder.finish()[-1] == "data: [DONE]\n\n"
