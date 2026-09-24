"""Shared HTTP plumbing for generic protocol drivers.

A driver that sets ``delegates_request_execution = True`` owns the full
exchange: URL construction, auth, timeout, response parsing and error
classification.  Router hands it ``base_url``/``endpoint``/``timeout``/
``headers``/``client``; Control Plane hands it a plain ctx (dict or
DriverContext) plus a credential.  Both call sites are supported without
duplicating logic in each protocol driver.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Mapping

import httpx

from apps.gateway.providers.driver_context import DriverContext
from apps.gateway.providers.error_classifier import classify_provider_error

# Hop-by-hop headers must never be forwarded upstream.
_FORBIDDEN_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def join_url(base_url: str, suffix: str) -> str:
    """Append an API suffix to a base URL without doubling slashes."""
    base = (base_url or "").rstrip("/")
    tail = (suffix or "").lstrip("/")
    if not tail:
        return base
    return f"{base}/{tail}"


def normalize_ctx(ctx: Any) -> DriverContext:
    """Accept dict, DriverContext or attribute-object ctx uniformly."""
    if isinstance(ctx, DriverContext):
        return ctx
    if isinstance(ctx, dict):
        return DriverContext.from_dict(ctx)
    if ctx is None:
        return DriverContext()
    # Attribute object (tests / legacy callers): read the known attributes.
    return DriverContext(
        connection_id=getattr(ctx, "connection_id", "") or "",
        base_url=getattr(ctx, "base_url", "") or "",
        driver=getattr(ctx, "driver", "") or "",
        template_id=getattr(ctx, "template_id", "") or "",
        credential=getattr(ctx, "credential", {}) or {},
        connection=getattr(ctx, "connection", {}) or {},
        runtime=getattr(ctx, "runtime", {}) or {},
    )


def default_client(timeout: float) -> httpx.AsyncClient:
    """Single construction point for driver-owned HTTP clients.

    Centralized so a test harness can install an offline transport globally
    instead of monkeypatching every driver class.
    """
    return httpx.AsyncClient(timeout=timeout)


def filter_forward_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {k: v for k, v in headers.items() if k.lower() not in _FORBIDDEN_HEADERS}


def credential_value(ctx: DriverContext, *names: str) -> str:
    """Pull the first present credential field, tolerating dict or str secret."""
    cred = ctx.credential
    if isinstance(cred, str):
        return cred if not names or names[0] in {"api_key", "token", "secret"} else ""
    if isinstance(cred, Mapping):
        for name in names:
            value = cred.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


class HttpExchangeMixin:
    """Real HTTP behavior shared by every generic protocol driver."""

    delegates_request_execution = True
    #: subclasses override with their protocol default suffix
    default_endpoint = ""
    discovery_endpoint = ""

    def _configure(
        self,
        *,
        base_url: str = "",
        endpoint: str | None = None,
        timeout: float = 60.0,
        headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url or ""
        self.endpoint = endpoint or self.default_endpoint
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._injected_client = client
        self._last_stream_usage: dict[str, Any] = {}

    def _client(self) -> httpx.AsyncClient:
        if self._injected_client is not None:
            return self._injected_client
        client = default_client(self.timeout)
        self._owned_client = client
        return client

    async def _close_owned_client(self) -> None:
        client = getattr(self, "_owned_client", None)
        if client is not None:
            self._owned_client = None
            await client.aclose()

    # ── request construction ───────────────────────────────────────────
    def _auth_headers(self, ctx: DriverContext) -> dict[str, str]:
        raise NotImplementedError

    def request_headers(self, ctx: DriverContext, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Filtered forwarded headers + protocol auth + per-request extras."""
        merged = filter_forward_headers(self.headers)
        merged.update(filter_forward_headers(extra))
        merged.update(self._auth_headers(ctx))
        return merged

    def target_url(self, ctx: DriverContext, suffix: str | None = None) -> str:
        base = self.base_url or ctx.base_url
        return join_url(base, suffix if suffix is not None else self.endpoint)

    # ── response handling ──────────────────────────────────────────────
    def _error_response(self, status_code: int, message: str, headers: Mapping[str, str] | None) -> dict[str, Any]:
        headers = dict(headers or {})
        return {
            "status_code": status_code,
            "headers": headers,
            "body": message,
            "usage": {},
            "classification": classify_provider_error(status_code=status_code, body=message, headers=headers),
        }

    def _response_dict(self, response: httpx.Response) -> dict[str, Any]:
        body = response.text
        try:
            payload = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            payload = {}
        usage = self.parse_usage(payload)
        classification = (
            classify_provider_error(status_code=response.status_code, body=payload or body, headers=response.headers)
            if response.status_code >= 400
            else {"kind": "SUCCESS", "retryable": False, "scope": None, "status_code": response.status_code}
        )
        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": body,
            "usage": usage,
            "classification": classification,
        }

    # ── control plane: real validation + discovery ─────────────────────
    async def validate_connection(self, ctx: Any) -> dict[str, Any]:
        """Probe the discovery endpoint so a bad URL/credential cannot pass."""
        context = normalize_ctx(ctx)
        url = self.target_url(context, self.discovery_endpoint)
        if not url:
            return {"status": "error", "kind": "CONFIG_INVALID", "detail": "base_url is required"}
        try:
            response = await self._client().get(url, headers=self.request_headers(context), timeout=self.timeout)
        except httpx.TimeoutException:
            return {"status": "error", "kind": "TIMEOUT", "detail": f"timeout after {self.timeout}s"}
        except httpx.HTTPError as exc:
            return {"status": "error", "kind": "TRANSIENT_NETWORK", "detail": type(exc).__name__}
        finally:
            await self._close_owned_client()
        if response.status_code >= 400:
            classification = classify_provider_error(
                status_code=response.status_code, body=response.text, headers=response.headers
            )
            return {
                "status": "error",
                "kind": classification.get("kind", "UNKNOWN"),
                "detail": f"discovery probe returned {response.status_code}",
                "retry_after": classification.get("retry_after"),
                "reset_at": classification.get("reset_at"),
            }
        return {"status": "ok", "detail": f"discovery probe {url} returned {response.status_code}"}

    async def discover_models(self, ctx: Any) -> list[Any]:
        """List models from the provider; raises ProviderDiscoveryError on failure."""
        context = normalize_ctx(ctx)
        url = self.target_url(context, self.discovery_endpoint)
        if not url:
            raise ProviderDiscoveryError("CONFIG_INVALID", "base_url is required", 400)
        try:
            response = await self._client().get(url, headers=self.request_headers(context), timeout=self.timeout)
        except httpx.TimeoutException:
            raise ProviderDiscoveryError("TIMEOUT", f"discovery timeout after {self.timeout}s", 504)
        except httpx.HTTPError as exc:
            raise ProviderDiscoveryError("TRANSIENT_NETWORK", type(exc).__name__, 502)
        finally:
            await self._close_owned_client()
        if response.status_code >= 400:
            classification = classify_provider_error(
                status_code=response.status_code, body=response.text, headers=response.headers
            )
            raise ProviderDiscoveryError(
                classification.get("kind", "UNKNOWN"),
                f"discovery probe {url} returned {response.status_code}",
                response.status_code,
            )
        return self.parse_discovery(response)

    def parse_discovery(self, response: httpx.Response) -> list[Any]:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return []
        return self._extract_model_ids(payload)

    def _extract_model_ids(self, payload: Any) -> list[Any]:
        for key in ("data", "models", "results"):
            if isinstance(payload, dict) and isinstance(payload.get(key), list):
                items = payload[key]
                break
        else:
            items = payload if isinstance(payload, list) else []
        out: list[Any] = []
        for item in items:
            if isinstance(item, str):
                out.append({"id": item})
            elif isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
                if model_id:
                    entry = dict(item)
                    entry["id"] = str(model_id).rsplit("/", 1)[-1]
                    out.append(entry)
        return out

    # ── data plane: execute (router delegate path) ─────────────────────
    async def execute(self, ctx: Any, request: Any) -> dict[str, Any]:
        """Perform one non-streaming upstream call and return the router contract dict."""
        context = normalize_ctx(ctx)
        url = self.target_url(context, getattr(request, "endpoint", None) or self.endpoint)
        extra = getattr(request, "headers", None)
        body = getattr(request, "body", None)
        payload = getattr(request, "json", None)
        method = getattr(request, "method", "POST") or "POST"
        try:
            response = await self._client().request(
                method,
                url,
                headers=self.request_headers(context, extra),
                content=body if body is not None else None,
                json=payload if body is None else None,
                timeout=self.timeout,
            )
        except httpx.TimeoutException:
            return self._error_response(408, "Request timeout", {})
        except httpx.HTTPError as exc:
            return self._error_response(0, f"upstream transport error: {type(exc).__name__}", {})
        finally:
            await self._close_owned_client()
        return self._response_dict(response)

    async def execute_stream(self, ctx: Any, request: Any) -> AsyncIterator[bytes]:
        """SSE / byte passthrough with bounded incremental usage parsing."""
        context = normalize_ctx(ctx)
        url = self.target_url(context, getattr(request, "endpoint", None) or self.endpoint)
        extra = getattr(request, "headers", None)
        body = getattr(request, "body", None)
        payload = getattr(request, "json", None)
        method = getattr(request, "method", "POST") or "POST"
        usage_parser = self._new_usage_parser()
        try:
            async with self._client().stream(
                method,
                url,
                headers=self.request_headers(context, extra),
                content=body if body is not None else None,
                json=payload if body is None else None,
                timeout=self.timeout,
            ) as response:
                if response.status_code >= 400:
                    error = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(error) < 64 * 1024:
                            error.extend(chunk[: 64 * 1024 - len(error)])
                    yield b""
                    self.last_stream_error = {
                        "status_code": response.status_code,
                        "body": error.decode("utf-8", errors="replace"),
                    }
                    return
                async for chunk in response.aiter_bytes():
                    if chunk:
                        usage_parser.feed(chunk)
                        yield chunk
                self._last_stream_usage = usage_parser.finish() or {}
        except httpx.TimeoutException:
            self._last_stream_usage = {}
            yield b""
        except httpx.HTTPError:
            self._last_stream_usage = {}
            yield b""
        finally:
            await self._close_owned_client()

    @property
    def last_stream_usage(self) -> dict[str, Any]:
        return getattr(self, "_last_stream_usage", {}) or {}

    def _new_usage_parser(self) -> "IncrementalSSEUsageParser":
        return IncrementalSSEUsageParser(self.parse_usage, self._merge_stream_usage)

    def parse_stream_usage(self, data: bytes) -> dict[str, Any]:
        """Default: scan SSE payload for the last JSON event carrying usage."""
        usage: dict[str, Any] = {}
        for event in iter_sse_events(data):
            try:
                payload = json.loads(event)
            except (json.JSONDecodeError, ValueError):
                continue
            parsed = self.parse_usage(payload)
            if parsed:
                usage = self._merge_stream_usage(usage, parsed)
        return usage

    def _merge_stream_usage(self, current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = dict(current)
        for key, value in incoming.items():
            if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                merged[key] = max(merged[key], value)
            else:
                merged[key] = value
        return merged

    def rate_limit_metadata(self, ctx: Any, headers: Mapping[str, str] | None) -> dict[str, Any]:
        """Protocol-neutral rate-limit header extraction (subclasses may extend)."""
        return extract_rate_limit_metadata(headers)


class IncrementalSSEUsageParser:
    """Parse complete SSE data lines while retaining only an incomplete tail."""

    def __init__(self, parse_usage: Any, merge_usage: Any) -> None:
        self._parse_usage = parse_usage
        self._merge_usage = merge_usage
        self._tail = bytearray()
        self._usage: dict[str, Any] = {}

    def feed(self, chunk: bytes) -> None:
        self._tail.extend(chunk)
        while True:
            newline = self._tail.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._tail[:newline]).rstrip(b"\r")
            del self._tail[: newline + 1]
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                parsed = self._parse_usage(json.loads(payload))
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if parsed:
                self._usage = self._merge_usage(self._usage, parsed)

    def finish(self) -> dict[str, Any]:
        if self._tail:
            self.feed(b"\n")
        return self._usage



def iter_sse_events(data: bytes) -> list[str]:
    """Return the ``data:`` payloads of an SSE body in arrival order."""
    events: list[str] = []
    for raw in (data or b"").decode("utf-8", errors="replace").split("\n"):
        line = raw.strip("\r")
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload and payload != "[DONE]":
                events.append(payload)
    return events


_RATE_LIMIT_HEADER_KEYS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
    "retry-after",
    "anthropic-ratelimit-requests-limit",
    "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-reset",
)


def extract_rate_limit_metadata(headers: Mapping[str, str] | None) -> dict[str, Any]:
    """Parse provider rate-limit headers into a neutral shape.

    Deliberately name-agnostic: any header ending in ``-limit``/``-remaining``/
    ``-reset`` is mapped onto the neutral key so new providers need no code change.
    """
    if not headers:
        return {}
    lowered = {str(k).lower(): v for k, v in headers.items()}
    out: dict[str, Any] = {}
    for key, value in lowered.items():
        if key not in _RATE_LIMIT_HEADER_KEYS and not key.endswith(("limit", "remaining", "reset")):
            continue
        if "remaining" in key:
            bucket = "remaining"
        elif key.endswith("limit") or "limit" in key:
            bucket = "limit"
        elif "reset" in key:
            bucket = "reset_at"
        else:
            continue
        if "token" in key:
            metric = "tokens"
        elif "request" in key:
            metric = "requests"
        else:
            metric = "requests"
        out.setdefault(bucket, {})[metric] = value
    if "retry-after" in lowered:
        out["retry_after"] = lowered["retry-after"]
    return out
