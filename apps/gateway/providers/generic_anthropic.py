from typing import Any, AsyncIterator, Dict, List, Optional
from apps.gateway.providers.base import ProviderDriver

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
        """Parse Anthropic usage from response body."""
        usage = response.get('usage', {})
        input_tokens = usage.get('input_tokens', 0)
        output_tokens = usage.get('output_tokens', 0)
        return {
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'total_tokens': input_tokens + output_tokens,
            'source': 'provider_api',
            'confidence': 'exact'
        }

    def classify_error(self, error_or_response: Any = None, status_code: Optional[int] = None, body: Optional[Dict] = None) -> Dict[str, Any]:
        """Classify Anthropic error responses."""
        # Support both positional error_or_response and keyword args
        if status_code is not None and body is not None:
            err_type = body.get('error', {}).get('type', '')
            err_message = body.get('error', {}).get('message', '')
        else:
            # Try to parse from error_or_response if provided
            if isinstance(error_or_response, dict):
                err_type = error_or_response.get('error', {}).get('type', '')
                status_code = error_or_response.get('status_code', 500)
            else:
                err_type = ''
                status_code = 500

        if status_code == 429 or 'rate_limit' in err_type.lower():
            return {'kind': 'RATE_LIMIT', 'retryable': True}
        elif status_code == 529 or 'overloaded' in err_type.lower():
            return {'kind': 'OVERLOADED', 'retryable': True}
        elif status_code == 401 or 'authentication' in err_type.lower() or 'auth' in err_type.lower():
            return {'kind': 'AUTH_EXPIRED', 'retryable': False}
        elif status_code == 404 or 'not_found' in err_type.lower():
            return {'kind': 'MODEL_NOT_FOUND', 'retryable': False}
        elif status_code >= 500:
            return {'kind': 'TRANSIENT_NETWORK', 'retryable': True}
        else:
            return {'kind': 'UNKNOWN', 'retryable': False}

    def capabilities(self) -> Dict[str, Any]:
        """Return driver capabilities."""
        return {
            'supports_streaming': True,
            'supports_tools': True,
            'supports_vision': True,
            'supports_structured_output': True
        }
