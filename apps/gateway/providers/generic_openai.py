from typing import Any, AsyncIterator, Dict
from apps.gateway.providers.base import ProviderDriver
from apps.gateway.providers.error_classifier import classify_provider_error

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
        """Parse OpenAI usage from response body.

        Returns an empty dict when ``usage`` is absent so callers can
        distinguish "no usage present" from "usage was zero".
        """
        if not isinstance(response, dict):
            return {}
        usage = response.get('usage') or {}
        if not usage:
            return {}
        input_tokens = usage.get('prompt_tokens', 0)
        output_tokens = usage.get('completion_tokens', 0)
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
        return {'supports_streaming': True, 'supports_tools': True}
