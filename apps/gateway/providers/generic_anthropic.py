"""Generic Anthropic-compatible driver.

Implements the P0-03 Anthropic surface: ``POST /v1/messages``, streaming,
``x-api-key`` + ``anthropic-version`` auth headers, usage parsing (including the
``message_start`` / ``message_delta`` streaming split), rate-limit header parsing
and error classification.  Provider-specific behaviour lives only in this driver
and in templates; the routing core never branches on a provider name.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from apps.gateway.providers.http_base import HttpExchangeMixin, credential_value
from apps.gateway.providers.error_classifier import classify_provider_error

DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


class GenericAnthropicDriver(HttpExchangeMixin):
    driver_id = "generic-anthropic"
    delegates_request_execution = True
    default_endpoint = "v1/messages"
    discovery_endpoint = "v1/models"

    def __init__(
        self,
        base_url: str = "",
        endpoint: str | None = None,
        timeout: float = 60.0,
        headers: Dict[str, str] | None = None,
        client: Any = None,
        api_key: str = "",
        anthropic_version: str | None = None,
    ) -> None:
        self._own_api_key = api_key or ""
        self.anthropic_version = (
            anthropic_version
            or os.getenv("ANTHROPIC_VERSION")
            or DEFAULT_ANTHROPIC_VERSION
        )
        self._configure(base_url=base_url, endpoint=endpoint, timeout=timeout, headers=headers, client=client)

    def _auth_headers(self, ctx: Any) -> Dict[str, str]:
        token = self._own_api_key or credential_value(ctx, "api_key", "x_api_key", "token", "secret")
        headers = {
            "content-type": "application/json",
            "anthropic-version": self.anthropic_version,
        }
        if token:
            headers["x-api-key"] = token
        return headers

    def parse_usage(self, response: Any) -> Dict[str, Any]:
        """Parse Anthropic usage.

        Streaming splits usage across events: ``message_start`` carries input
        tokens under ``message.usage`` while ``message_delta`` carries output
        tokens at the top level.  Non-streaming responses keep both at top level.
        Returns ``{}`` when no usage is present so callers can distinguish
        "absent" from "zero".
        """
        if not isinstance(response, dict):
            return {}
        usage = response.get("usage") or {}
        if not usage:
            usage = (response.get("message") or {}).get("usage") or {}
        if not usage:
            return {}
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        parsed = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": usage.get(
                "total_tokens", int(input_tokens or 0) + int(output_tokens or 0)
            ),
            "source": "provider_api",
            "confidence": "exact",
        }
        cache_read = usage.get("cache_read_input_tokens")
        if cache_read is not None:
            parsed["cache_read_input_tokens"] = cache_read
        cache_write = usage.get("cache_creation_input_tokens")
        if cache_write is not None:
            parsed["cache_creation_input_tokens"] = cache_write
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
            "supports_vision": True,
            "supports_structured_output": True,
            "supports_discovery": True,
            "supports_usage": True,
            "supports_rate_limit_metadata": True,
        }
