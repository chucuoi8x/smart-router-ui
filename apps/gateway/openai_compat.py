import json
from typing import Any, Iterable


def openai_request_to_router(body: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI Chat Completions request into router's Messages input."""
    out: dict[str, Any] = {"model": body.get("model"), "messages": []}
    if "max_completion_tokens" in body:
        out["max_tokens"] = body["max_completion_tokens"]
    elif "max_tokens" in body:
        out["max_tokens"] = body["max_tokens"]
    for key in ("temperature", "top_p", "stream", "stop", "metadata"):
        if key in body:
            out[key] = body[key]

    system_parts: list[str] = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role in ("system", "developer"):
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue
        if role == "tool":
            content = json.dumps({"tool_call_id": msg.get("tool_call_id"), "result": content}, ensure_ascii=False)
            role = "user"
        if role not in ("user", "assistant"):
            continue
        out["messages"].append({"role": role, "content": content or ""})
    if system_parts:
        out["system"] = "\n".join(system_parts)
    if not out["messages"]:
        out["messages"] = [{"role": "user", "content": ""}]
    return out


def router_response_to_openai(payload: dict[str, Any], requested_model: str) -> dict[str, Any]:
    """Normalize native Messages or already-OpenAI payload into Chat Completions."""
    if payload.get("object") == "chat.completion" and isinstance(payload.get("choices"), list):
        return payload
    content = payload.get("content", "")
    if isinstance(content, list):
        content = "".join(str(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("type", "text") == "text")
    if not isinstance(content, str):
        content = str(content or "")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    prompt = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    completion = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    finish = "length" if payload.get("stop_reason") in ("max_tokens", "length") else "stop"
    return {
        "id": payload.get("id", "chatcmpl-smart-router"),
        "object": "chat.completion",
        "created": int(payload.get("created", 0) or 0),
        "model": payload.get("model", requested_model),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
    }


class OpenAIStreamEncoder:
    """Incrementally translate Anthropic-style SSE events to OpenAI SSE chunks."""

    def __init__(self, requested_model: str) -> None:
        self.requested_model = requested_model
        self.response_id = "chatcmpl-smart-router"
        self.response_model = requested_model
        self.started = False
        self.finished = False

    def feed(self, event: dict[str, Any]) -> list[str]:
        if self.finished or event.get("type") == "ping":
            return []
        kind = event.get("type")
        if kind == "message_start" and not self.started:
            msg = event.get("message") if isinstance(event.get("message"), dict) else {}
            self.started = True
            self.response_id = str(msg.get("id") or self.response_id)
            self.response_model = str(msg.get("model") or self.response_model)
            chunk = {"id": self.response_id, "object": "chat.completion.chunk", "created": 0, "model": self.response_model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
            return ["data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n"]
        if kind == "content_block_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            text = delta.get("text", "")
            if text:
                chunk = {"id": self.response_id, "object": "chat.completion.chunk", "created": 0, "model": self.response_model, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                return ["data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n"]
        if kind == "message_delta":
            reason = (event.get("delta") or {}).get("stop_reason")
            if reason:
                chunk = {"id": self.response_id, "object": "chat.completion.chunk", "created": 0, "model": self.response_model, "choices": [{"index": 0, "delta": {}, "finish_reason": "length" if reason in ("max_tokens", "length") else "stop"}]}
                return ["data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n"]
        return []

    def finish(self) -> list[str]:
        if self.finished:
            return []
        self.finished = True
        return ["data: [DONE]\n\n"]


def router_stream_to_openai(events: Iterable[dict[str, Any]], requested_model: str):
    """Yield OpenAI SSE chunks from Messages-stream events."""
    encoder = OpenAIStreamEncoder(requested_model)
    for event in events:
        yield from encoder.feed(event)
    yield from encoder.finish()
