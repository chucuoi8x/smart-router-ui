"""Generic OpenAI-compatible driver.

Implements the real protocol surface required by SMART_ROUTER_P0_STABILIZATION_PLAN
P0-03: model discovery (``GET /models``), chat completion execution
(``POST /chat/completions``), SSE streaming, Bearer auth, custom headers, custom
base URL, timeout, usage parsing, rate-limit header parsing and error
classification.  Nothing here branches on a provider name: any OpenAI-compatible
endpoint is reachable purely from Control Plane configuration.
"""
from __future__ import annotations

from typing import Any, Dict

from apps.gateway.providers.http_base import HttpExchangeMixin, credential_value
from apps.gateway.providers.error_classifier import classify_provider_error


class GenericOpenAIDriver(HttpExchangeMixin):
    driver_id = "generic-openai"
    # Data plane currently reaches OpenAI-compatible upstreams through the
    # router's direct client; delegation is enabled deliberately, per driver,
    # once the Control Plane registry can hand over a resolved credential.
    delegates_request_execution = False
    default_endpoint = "chat/completions"
    discovery_endpoint = "models"

    def __init__(
        self,
        base_url: str = "",
        endpoint: str | None = None,
        timeout: float = 60.0,
        headers: Dict[str, str] | None = None,
        client: Any = None,
        api_key: str = "",
    ) -> None:
        self._own_api_key = api_key or ""
        self._configure(base_url=base_url, endpoint=endpoint, timeout=timeout, headers=headers, client=client)

    def _auth_headers(self, ctx: Any) -> Dict[str, str]:
        token = self._own_api_key or credential_value(ctx, "api_key", "token", "secret")
        headers = {"content-type": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        return headers

    def parse_usage(self, response: Any) -> Dict[str, Any]:
        """Parse OpenAI usage; ``{}`` when the provider omitted it entirely."""
        if not isinstance(response, dict):
            return {}
        usage = response.get("usage") or {}
        if not usage:
            return {}
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        parsed = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": usage.get("total_tokens", input_tokens + output_tokens),
            "source": "provider_api",
            "confidence": "exact",
        }
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            parsed["cached_tokens"] = details["cached_tokens"]
        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, dict) and completion_details.get("reasoning_tokens") is not None:
            parsed["reasoning_tokens"] = completion_details["reasoning_tokens"]
        return parsed

    def classify_error(
        self,
        error_or_response: Any = None,
        status_code: int | None = None,
        body: dict | None = None,
        headers: dict | None = None,
    ) -> Dict[str, Any]:
        if body is None and isinstance(error_or_response, dict):
            body = error_or_response
            status_code = status_code or error_or_response.get("status_code")
            headers = headers or error_or_response.get("headers")
        return classify_provider_error(status_code=status_code, body=body, headers=headers)

    def capabilities(self) -> Dict[str, Any]:
        return {
            "supports_streaming": True,
            "supports_tools": True,
            "supports_discovery": True,
            "supports_usage": True,
            "supports_rate_limit_metadata": True,
        }
