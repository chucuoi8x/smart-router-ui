"""Recursive secret redaction for provider, audit, and health payloads."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_SECRET_KEYS = {
    "api_key",
    "credential",
    "credential_encrypted",
    "secret",
    "secret_key",
    "secret_key_hash",
    "secret_encrypted",
    "token",
}


def redact_secrets(value: Any) -> Any:
    """Remove secret fields from any JSON-like structure without mutating input."""
    if isinstance(value, Mapping):
        return {
            key: redact_secrets(item)
            for key, item in value.items()
            if str(key).lower() not in _SECRET_KEYS
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_secrets(item) for item in value]
    return value
