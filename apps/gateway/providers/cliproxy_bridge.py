from typing import Any, AsyncIterator, Dict, Optional
import json
import httpx

from apps.gateway.providers.base import ProviderDriver
from apps.gateway.providers.error_classifier import classify_provider_error


class CLIProxyBridgeDriver(ProviderDriver):
    """Provider driver that forwards requests to a CLIProxy sidecar/daemon."""

    driver_id = "cliproxy-bridge"

    def __init__(
        self,
        base_url: str,
        endpoint: str = "/",
        timeout: float = 60.0,
        headers: Optional[Dict[str, str]] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint.lstrip("/")
        self.timeout = timeout
        self.headers = headers or {}
        self._client = client
        self._owns_client = client is None
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=timeout)
        self._last_stream_usage: Dict[str, Any] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _filter_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """Remove hop-by-hop headers that should not be forwarded."""
        forbidden = {
            "host",
            "content-length",
            "connection",
            "keep-alive",
            "transfer-encoding",
            "expect",
        }
        return {k: v for k, v in headers.items() if k.lower() not in forbidden}

    def _build_url(self) -> str:
        return f"{self.base_url}/{self.endpoint}"

    def _parse_usage_from_body(self, body: Any) -> Dict[str, Any]:
        """Extract token/cost usage from a response body (JSON or SSE)."""
        if not body:
            return {}
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError:
                return {}
        if isinstance(body, str):
            # Check if it's SSE (Server-Sent Events) format
            if "data:" in body:
                return self._parse_usage_from_sse(body)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return {}
        elif isinstance(body, dict):
            data = body
        else:
            return {}

        usage = data.get("usage")
        if not isinstance(usage, dict):
            return {}

        result: Dict[str, Any] = {}
        for field in ("input_tokens", "output_tokens", "total_tokens", "cost"):
            if field in usage:
                result[field] = usage[field]
        # Preserve any extra fields
        for k, v in usage.items():
            if k not in result:
                result[k] = v
        return result

    def _parse_usage_from_sse(self, sse_text: str) -> Dict[str, Any]:
        """Parse usage from SSE (Server-Sent Events) data stream."""
        usage: Dict[str, Any] = {}
        for line in sse_text.splitlines():
            if not line.startswith("data: "):
                continue
            data_payload = line[6:]  # Remove "data: " prefix
            if data_payload == "[DONE]":
                continue
            try:
                data = json.loads(data_payload)
            except json.JSONDecodeError:
                continue
            if "usage" in data and isinstance(data["usage"], dict):
                # Update with latest usage found
                for k, v in data["usage"].items():
                    usage[k] = v
        return usage

    def _parse_usage_from_headers(self, headers: Dict[str, str]) -> Dict[str, Any]:
        """Extract usage from provider-specific headers."""
        usage: Dict[str, Any] = {}
        for key, value in headers.items():
            lower = key.lower()
            if lower.startswith("x-usage-"):
                metric = lower.replace("x-usage-", "")
                usage[metric] = value
            elif lower == "x-ratelimit-remaining-tokens":
                usage["remaining_tokens"] = value
            elif lower == "x-ratelimit-limit-tokens":
                usage["limit_tokens"] = value
        return usage

    async def execute(self, ctx: Any, request: Any) -> Any:
        """Execute a non-streaming request by forwarding it to the CLIProxy service."""
        url = self._build_url()
        method = getattr(request, "method", "POST")
        req_headers = getattr(request, "headers", {})
        filtered_headers = self._filter_headers(req_headers)
        final_headers = {**filtered_headers, **self.headers}
        body = getattr(request, "body", None)
        json_data = getattr(request, "json", None)

        try:
            response = await self._client.request(
                method=method,
                url=url,
                headers=final_headers,
                content=body,
                json=json_data,
                timeout=self.timeout,
            )
        except httpx.TimeoutException:
            return self._build_error_response(408, "Request timeout", {})
        except Exception as e:
            return self._build_error_response(500, f"Request failed: {e}", {})

        usage = self._parse_usage_from_body(response.text)
        classification = classify_provider_error(
            status_code=response.status_code,
            body=response.text,
            headers=response.headers,
        )

        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response.text,
            "usage": usage,
            "classification": classification,
        }

    async def execute_stream(self, ctx: Any, request: Any) -> AsyncIterator[bytes]:
        """Execute a streaming request, yielding chunks as they arrive."""
        url = self._build_url()
        method = getattr(request, "method", "POST")
        req_headers = getattr(request, "headers", {})
        filtered_headers = self._filter_headers(req_headers)
        final_headers = {**filtered_headers, **self.headers}
        body = getattr(request, "body", None)
        json_data = getattr(request, "json", None)

        # Accumulate chunks for usage parsing at the end.
        # We buffer so we can parse usage after the stream finishes.
        accumulated = bytearray()

        try:
            async with self._client.stream(
                method=method,
                url=url,
                headers=final_headers,
                content=body,
                json=json_data,
                timeout=self.timeout,
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    error_text = error_body.decode("utf-8", errors="replace")
                    classification = classify_provider_error(
                        status_code=response.status_code,
                        body=error_text,
                        headers=response.headers,
                    )
                    # Yield nothing on error; caller can inspect classification via the returned dict?
                    # For now, we yield an empty chunk to signal error.
                    yield b""
                    return

                async for chunk in response.aiter_bytes():
                    if chunk:
                        accumulated.extend(chunk)
                        yield chunk

                # After stream ends, parse usage from accumulated body and headers.
                usage = self._parse_usage_from_body(bytes(accumulated))
                header_usage = self._parse_usage_from_headers(dict(response.headers))
                usage.update(header_usage)
                self._last_stream_usage = usage

        except httpx.TimeoutException:
            # Could yield error chunk? We'll yield nothing and set usage empty.
            self._last_stream_usage = {}
            yield b""
        except Exception:
            self._last_stream_usage = {}
            yield b""

    def _build_error_response(self, status_code: int, message: str, headers: Dict[str, str]) -> Dict[str, Any]:
        classification = classify_provider_error(
            status_code=status_code,
            body=message,
            headers=headers,
        )
        return {
            "status_code": status_code,
            "headers": headers,
            "body": message,
            "usage": {},
            "classification": classification,
        }

    def parse_usage(self, response: Any) -> Dict[str, Any]:
        """Extract usage from a response object or dict."""
        if isinstance(response, dict):
            # If it's a dict from execute(), it may have 'body' or 'usage' field.
            if "usage" in response and response["usage"]:
                return dict(response["usage"])
            if "body" in response:
                return self._parse_usage_from_body(response["body"])
        # If it's a response object with .text
        if hasattr(response, "text"):
            return self._parse_usage_from_body(response.text)
        # Fallback to stored streaming usage
        return self._last_stream_usage.copy()

    def classify_error(
        self,
        error_or_response: Any = None,
        status_code: int = None,
        body: Any = None,
        headers: Dict[str, str] = None,
    ) -> Dict[str, Any]:
        if body is None and isinstance(error_or_response, dict):
            body = error_or_response.get("body")
            status_code = status_code or error_or_response.get("status_code")
            headers = headers or error_or_response.get("headers")
        return classify_provider_error(status_code=status_code, body=body, headers=headers)

    async def validate_connection(self, ctx: Any) -> Any:
        """Check connectivity to the CLIProxy service."""
        # Simple health check by sending a HEAD request to the base URL.
        try:
            # We could also try to get models, but a simple ping is enough.
            # For now, assume it's reachable if we can connect.
            # Use a short timeout.
            async with self._client.stream("HEAD", self.base_url, timeout=5.0) as resp:
                # Any response is fine.
                return {"status": "ok", "status_code": resp.status_code}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def discover_models(self, ctx: Any) -> list[Any]:
        """Retrieve available models from the CLIProxy service."""
        try:
            url = f"{self.base_url}/models"
            response = await self._client.get(url, timeout=5.0)
            if response.status_code == 200:
                data = response.json()
                # Expect a list of models under "models" key or a direct list.
                if isinstance(data, dict) and "models" in data:
                    return data["models"]
                elif isinstance(data, list):
                    return data
                else:
                    return []
            else:
                return []
        except Exception:
            return []

    async def fetch_quota(self, ctx: Any) -> list[Any]:
        """Fetch quota information from the CLIProxy service."""
        try:
            url = f"{self.base_url}/quota"
            response = await self._client.get(url, timeout=5.0)
            if response.status_code == 200:
                data = response.json()
                # Expect a list under "resources" key or direct list.
                if isinstance(data, dict) and "resources" in data:
                    return data["resources"]
                elif isinstance(data, list):
                    return data
                else:
                    return []
            else:
                return []
        except Exception:
            return []

    def capabilities(self) -> Dict[str, Any]:
        """Return driver capabilities."""
        return {"supports_streaming": True}