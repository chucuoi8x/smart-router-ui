"""Generic Gemini-compatible driver.

Implements the P0-03 Gemini surface: model discovery (``GET /v1beta/models``),
``generateContent`` execution, ``streamGenerateContent`` streaming,
``usageMetadata`` parsing, and quota/rate metadata extraction.  Provider-specific
behavior lives only in this driver; the routing core never branches on a
provider name.
"""
from __future__ import annotations

import json
from typing import Any, Dict, AsyncIterator

from apps.gateway.providers.http_base import HttpExchangeMixin, credential_value, normalize_ctx
from apps.gateway.providers.error_classifier import classify_provider_error


class GenericGeminiDriver(HttpExchangeMixin):
    driver_id = "generic-gemini"
    delegates_request_execution = True
    default_endpoint = "generateContent"
    discovery_endpoint = "v1beta/models"

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
            headers["x-goog-api-key"] = token
        return headers

    def target_url(self, ctx: Any, suffix: str | None = None) -> str:
        """Gemini requires model ID in the URL path for generateContent."""
        base = super().target_url(ctx, suffix)
        if suffix in (None, "generateContent", "streamGenerateContent"):
            model_id = self._extract_model_id(ctx)
            if model_id:
                base = f"{base.rstrip('/')}/models/{model_id}:{suffix or 'generateContent'}"
        return base

    async def execute(self, ctx: Any, request: Any) -> dict[str, Any]:
        """Inject model_id from request body into ctx.runtime before URL build."""
        context = normalize_ctx(ctx)
        model_id = self._extract_model_from_request(request)
        if model_id:
            context.runtime["model_id"] = model_id
        return await super().execute(context, request)

    async def execute_stream(self, ctx: Any, request: Any) -> AsyncIterator[bytes]:
        """Inject model_id from request body into ctx.runtime before URL build."""
        context = normalize_ctx(ctx)
        model_id = self._extract_model_from_request(request)
        if model_id:
            context.runtime["model_id"] = model_id
        async for chunk in super().execute_stream(context, request):
            yield chunk

    def _extract_model_from_request(self, request: Any) -> str:
        """Parse model_id from request body JSON."""
        body = getattr(request, "body", None)
        if body is None:
            return ""
        try:
            if isinstance(body, bytes):
                body = body.decode("utf-8")
            if isinstance(body, str):
                payload = json.loads(body)
            else:
                payload = body
            return payload.get("model_id") or payload.get("model") or ""
        except (json.JSONDecodeError, ValueError):
            return ""

    def _extract_model_id(self, ctx: Any) -> str:
        """Extract model ID from ctx (connection, credential, or runtime)."""
        if isinstance(ctx, dict):
            return ctx.get("model_id") or ctx.get("model") or ""
        # Check runtime dict first (injected by execute override)
        runtime = getattr(ctx, "runtime", None)
        if isinstance(runtime, dict) and runtime.get("model_id"):
            return runtime["model_id"]
        return getattr(ctx, "model_id", "") or getattr(ctx, "model", "") or ""

    def parse_usage(self, response: Any) -> Dict[str, Any]:
        """Parse Gemini usageMetadata.

        Gemini returns usage under ``usageMetadata`` with ``promptTokenCount``
        and ``candidatesTokenCount``.  Returns ``{}`` when absent so callers can
        distinguish "absent" from "zero".
        """
        if not isinstance(response, dict):
            return {}
        usage = response.get("usageMetadata") or {}
        if not usage:
            return {}
        input_tokens = usage.get("promptTokenCount", 0)
        output_tokens = usage.get("candidatesTokenCount", 0)
        parsed = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": usage.get(
                "totalTokenCount", int(input_tokens or 0) + int(output_tokens or 0)
            ),
            "source": "provider_api",
            "confidence": "exact",
        }
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
            "supports_discovery": True,
            "supports_usage": True,
            "supports_rate_limit_metadata": True,
        }
