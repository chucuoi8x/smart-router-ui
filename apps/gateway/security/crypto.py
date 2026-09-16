"""Fernet-backed credential encryption.

Production deployments must set SMART_ROUTER_ENCRYPTION_KEY to a Fernet key.
The deterministic development fallback keeps local tests/restarts usable while
never storing plaintext in provider records or API responses.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    raw = os.getenv("SMART_ROUTER_ENCRYPTION_KEY", "")
    if raw:
        try:
            return Fernet(raw.encode("ascii"))
        except Exception as exc:
            raise RuntimeError("SMART_ROUTER_ENCRYPTION_KEY must be a valid Fernet key") from exc
    # Development-only fallback. Production must configure a key explicitly.
    seed = os.getenv("SMART_ROUTER_DEV_ENCRYPTION_SEED", "smart-router-local-development-key")
    key = base64.urlsafe_b64encode(hashlib.sha256(seed.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("secret must be a non-empty string")
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    if not isinstance(ciphertext, str) or not ciphertext:
        raise ValueError("ciphertext must be a non-empty string")
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid encrypted secret") from exc
