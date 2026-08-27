from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

_QUOTA_TERMS = (
    "quota_exhausted",
    "insufficient_quota",
    "quota exceeded",
    "quota exhausted",
    "monthly quota",
    "daily quota",
    "billing quota",
    "credit exhausted",
    "credits exhausted",
    "insufficient credit",
    "insufficient balance",
)
_CONTEXT_TERMS = (
    "context_length_exceeded",
    "context too large",
    "maximum context",
    "token limit",
)
_CONTENT_POLICY_TERMS = (
    "content_policy",
    "safety",
    "blocked by policy",
)


def _headers_lookup(headers: Mapping[str, Any] | None, name: str) -> str | None:
    if not headers or not isinstance(headers, Mapping):
        return None
    needle = name.lower()
    for key, value in headers.items():
        if str(key).lower() == needle:
            return str(value)
    return None


def _safe_body_text(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, str):
        return body.lower()
    try:
        return json.dumps(body, sort_keys=True).lower()
    except Exception:
        return str(body).lower()


def _provider_code(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    value = err.get("code") or err.get("type") or body.get("code") or body.get("type")
    return str(value) if value else None


def _safe_message(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    value = err.get("message") or body.get("message")
    if not value:
        return None
    message = str(value)
    return message[:300]


def _retry_after(headers: Mapping[str, Any] | None) -> str | None:
    return _headers_lookup(headers, "retry-after")


def _reset_at(headers: Mapping[str, Any] | None) -> str | None:
    for name in (
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
        "x-ratelimit-reset",
        "x-quota-reset",
        "ratelimit-reset",
    ):
        value = _headers_lookup(headers, name)
        if value:
            return value
    return None


def _result(
    kind: str,
    *,
    retryable: bool,
    scope: str,
    status_code: int | None,
    body: Any,
    headers: Mapping[str, Any] | None,
    consumption_uncertainty: str = "unknown",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "retryable": retryable,
        "scope": scope,
        "retry_after": _retry_after(headers),
        "reset_at": _reset_at(headers),
        "consumption_uncertainty": consumption_uncertainty,
        "provider_error_code": _provider_code(body),
        "safe_message": _safe_message(body),
        "status_code": status_code,
    }


def classify_provider_error(
    *,
    status_code: int | None = None,
    body: Any = None,
    headers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize common provider failures into Smart Router error kinds.

    Quota exhaustion is separated from short-lived rate limits because quota
    state should block a resource until replenishment, while rate limits should
    cool down and retry elsewhere.
    """
    text = _safe_body_text(body)
    code = (_provider_code(body) or "").lower()
    combined = f"{code} {text}"

    if any(term in combined for term in _QUOTA_TERMS):
        return _result(
            "QUOTA_EXHAUSTED",
            retryable=False,
            scope="account",
            status_code=status_code,
            body=body,
            headers=headers,
            consumption_uncertainty="low",
        )

    if status_code == 429 or "rate_limit" in combined or "rate limit" in combined:
        return _result(
            "RATE_LIMIT",
            retryable=True,
            scope="credential/model/connection",
            status_code=status_code,
            body=body,
            headers=headers,
        )

    if status_code == 529 or "overloaded" in combined:
        return _result(
            "OVERLOADED",
            retryable=True,
            scope="model/provider",
            status_code=status_code,
            body=body,
            headers=headers,
        )

    if status_code == 401 or "authentication" in combined or "invalid api key" in combined:
        return _result(
            "AUTH_EXPIRED",
            retryable=False,
            scope="credential",
            status_code=status_code,
            body=body,
            headers=headers,
        )

    if status_code == 403 and any(term in combined for term in _CONTENT_POLICY_TERMS):
        return _result(
            "CONTENT_POLICY",
            retryable=False,
            scope="request/provider",
            status_code=status_code,
            body=body,
            headers=headers,
            consumption_uncertainty="none",
        )

    if status_code == 403:
        return _result(
            "AUTH_REVOKED",
            retryable=False,
            scope="credential",
            status_code=status_code,
            body=body,
            headers=headers,
        )

    if status_code == 404 or "not_found" in combined or "model not found" in combined:
        return _result(
            "MODEL_NOT_FOUND",
            retryable=False,
            scope="model",
            status_code=status_code,
            body=body,
            headers=headers,
        )

    if any(term in combined for term in _CONTEXT_TERMS):
        return _result(
            "CONTEXT_TOO_LARGE",
            retryable=False,
            scope="request/model",
            status_code=status_code,
            body=body,
            headers=headers,
            consumption_uncertainty="none",
        )

    if status_code == 400:
        return _result(
            "INVALID_REQUEST",
            retryable=False,
            scope="request",
            status_code=status_code,
            body=body,
            headers=headers,
            consumption_uncertainty="none",
        )

    if status_code is not None and status_code >= 500:
        return _result(
            "TRANSIENT_NETWORK",
            retryable=True,
            scope="connection",
            status_code=status_code,
            body=body,
            headers=headers,
        )

    return _result(
        "UNKNOWN",
        retryable=False,
        scope="attempt",
        status_code=status_code,
        body=body,
        headers=headers,
    )
