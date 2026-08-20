from typing import Any, AsyncIterator, Dict
from apps.gateway.providers.base import ProviderDriver

class CLIProxyBridgeDriver(ProviderDriver):
    driver_id = 'cliproxy-bridge'

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
        return {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}

    def classify_error(self, error_or_response: Any = None, status_code: int = None, body: dict = None) -> Dict[str, Any]:
        if status_code == 429:
            return {'kind': 'RATE_LIMIT', 'retryable': True}
        else:
            return {'kind': 'UNKNOWN', 'retryable': False}

    def capabilities(self) -> Dict[str, Any]:
        return {'supports_streaming': True}
