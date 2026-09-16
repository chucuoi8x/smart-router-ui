"""Runtime quota/error state normalization for AC-07.

Tach rate-limit ngan han khoi quota exhaustion dai han, giu metadata
provider-agnostic de status API va telemetry dung chung.
"""
from __future__ import annotations

from typing import Any


_RATE_LIMIT = "RATE_LIMIT"
_QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"


def describe_runtime_state(
    kind: str,
    *,
    reset_at: str | None = None,
    retry_after: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """Return stable runtime state metadata for normalized provider errors."""
    normalized = str(kind or "UNKNOWN").strip().upper() or "UNKNOWN"
    if normalized == _RATE_LIMIT:
        return {
            "state": "rate_limited",
            "kind": normalized,
            "retryable": True,
            "is_long_term": False,
            "reset_at": reset_at,
            "retry_after": retry_after,
            "scope": scope or "credential/model/connection",
        }
    if normalized == _QUOTA_EXHAUSTED:
        return {
            "state": "quota_exhausted",
            "kind": normalized,
            "retryable": False,
            "is_long_term": True,
            "reset_at": reset_at,
            "retry_after": retry_after,
            "scope": scope or "account",
        }
    return {
        "state": "unknown",
        "kind": normalized,
        "retryable": False,
        "is_long_term": False,
        "reset_at": reset_at,
        "retry_after": retry_after,
        "scope": scope,
    }
