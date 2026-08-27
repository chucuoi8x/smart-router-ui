from typing import Any, AsyncIterator, Dict, List, Optional
from apps.gateway.providers.base import ProviderDriver
from apps.gateway.providers.error_classifier import classify_provider_error

class GenericAnthropicDriver(ProviderDriver):
    driver_id = "generic-anthropic"

    async def validate_connection(self, ctx: Any) -> Any:
        """Stub: validate connection."""
        return {"status": "ok"}

    async def discover_models(self, ctx: Any) -> list[Any]:
        """Stub: discover models."""
        return []

    async def execute(self, ctx: Any, request: Any) -> Any:
        """Stub: execute request."""
        return {"content": "stub"}

    async def execute_stream(self, ctx: Any, request: Any) -> AsyncIterator[bytes]:
        """Stub: execute stream."""
        async def gen():
            yield b""
        return gen()

    async def fetch_quota(self, ctx: Any) -> list[Any]:
        """Stub: fetch quota."""
        return []

    def parse_usage(self, response: Any) -> Dict[str, Any]:
        """Parse Anthropic usage from response body.

        In streaming mode usage may appear at different nesting levels:
          - ``message_start`` puts usage under ``message.usage`` (input tokens)
          - ``message_delta`` puts usage at top level (output tokens)
        Non-streaming responses always put usage at the top level.

        When no ``usage`` key is found anywhere, return an empty dict so
        callers can distinguish "no usage present" from "usage was zero".
        """
        if not isinstance(response, dict):
            return {}

        # Top-level usage (message_delta, non-streaming response)
        usage = response.get('usage') or {}
        # Nested usage (message_start)
        if not usage:
            usage = (response.get('message') or {}).get('usage') or {}
        if not usage:
            return {}

        input_tokens = usage.get('input_tokens', 0)
        output_tokens = usage.get('output_tokens', 0)
        return {
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'total_tokens': input_tokens + output_tokens,
            'source': 'provider_api',
            'confidence': 'exact'
        }

    def classify_error(
        self,
        error_or_response: Any = None,
        status_code: Optional[int] = None,
        body: Optional[Dict] = None,
        headers: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Classify Anthropic error responses."""
        if body is None and isinstance(error_or_response, dict):
            body = error_or_response
            status_code = status_code or error_or_response.get('status_code')
            headers = headers or error_or_response.get('headers')
        return classify_provider_error(status_code=status_code, body=body, headers=headers)

    def capabilities(self) -> Dict[str, Any]:
        """Return driver capabilities."""
        return {
            'supports_streaming': True,
            'supports_tools': True,
            'supports_vision': True,
            'supports_structured_output': True
        }
