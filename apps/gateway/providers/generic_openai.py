from typing import Any, AsyncIterator, Dict
from apps.gateway.providers.base import ProviderDriver

class GenericOpenAIDriver(ProviderDriver):
    driver_id = 'generic-openai'

    async def validate_connection(self, ctx: Any) -> Any:
        return {'status': 'ok'}

    async def discover_models(self, ctx: Any) -> list[Any]:
        return []

    async def execute(self, ctx: Any, request: Any) -> Any:
        return {'content': 'stub'}

    async def execute_stream(self, ctx: Any, request: Any) -> AsyncIterator[bytes]:
        async def gen():
            yield b''
        return gen()

    async def fetch_quota(self, ctx: Any) -> list[Any]:
        return []

    def parse_usage(self, response: Any) -> Dict[str, Any]:
        usage = response.get('usage', {})
        input_tokens = usage.get('prompt_tokens', 0)
        output_tokens = usage.get('completion_tokens', 0)
        return {
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'total_tokens': input_tokens + output_tokens,
            'source': 'provider_api',
            'confidence': 'exact'
        }

    def classify_error(self, error_or_response: Any = None, status_code: int = None, body: dict = None) -> Dict[str, Any]:
        if status_code == 429:
            return {'kind': 'RATE_LIMIT', 'retryable': True}
        elif status_code >= 500:
            return {'kind': 'TRANSIENT_NETWORK', 'retryable': True}
        elif status_code == 401:
            return {'kind': 'AUTH_EXPIRED', 'retryable': False}
        else:
            return {'kind': 'UNKNOWN', 'retryable': False}

    def capabilities(self) -> Dict[str, Any]:
        return {'supports_streaming': True, 'supports_tools': True}
