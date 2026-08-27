from typing import Any, AsyncIterator, Dict
from apps.gateway.providers.base import ProviderDriver
from apps.gateway.providers.error_classifier import classify_provider_error

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

    def classify_error(
        self,
        error_or_response: Any = None,
        status_code: int = None,
        body: dict = None,
        headers: dict = None,
    ) -> Dict[str, Any]:
        if body is None and isinstance(error_or_response, dict):
            body = error_or_response
            status_code = status_code or error_or_response.get('status_code')
            headers = headers or error_or_response.get('headers')
        return classify_provider_error(status_code=status_code, body=body, headers=headers)

    def capabilities(self) -> Dict[str, Any]:
        return {'supports_streaming': True}
