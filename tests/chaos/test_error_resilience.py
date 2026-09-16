"""Chaos tests Step 134 — upstream failure resilience (M7)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.providers.error_classifier import classify_provider_error


def test_upstream_5xx_classifies_as_retryable_transient():
    result = classify_provider_error(status_code=502, body={"error": "bad gateway"})
    assert result["kind"] == "TRANSIENT_NETWORK"
    assert result["retryable"] is True


def test_upstream_503_sets_overloaded_state():
    result = classify_provider_error(status_code=503, body={"error": "service unavailable"})
    assert result["retryable"] is True
    assert result["kind"] == "TRANSIENT_NETWORK"


def test_rate_limit_429_has_retry_after_and_is_not_permanent():
    result = classify_provider_error(status_code=429, body={"error": "slow down"}, headers={"retry-after": "30"})
    assert result["retryable"] is True
    assert result.get("retry_after") == "30"
    assert result["kind"] == "RATE_LIMIT"


def test_quota_exhaustion_is_distinct_from_rate_limit():
    limited = classify_provider_error(status_code=429, body={"error": {"code": "rate_limit_exceeded"}})
    exhausted = classify_provider_error(status_code=429, body={"error": {"code": "insufficient_quota"}})
    assert limited["kind"] != exhausted["kind"]
    assert exhausted["kind"] == "QUOTA_EXHAUSTED"
    assert exhausted["retryable"] is False


def test_invalid_request_is_permanent_and_not_retryable():
    result = classify_provider_error(status_code=400, body={"error": {"type": "invalid_request_error"}})
    assert result["retryable"] is False
    assert result["kind"] == "INVALID_REQUEST"


def test_context_too_large_is_permanent_for_candidate():
    result = classify_provider_error(status_code=400, body={"error": {"code": "context_length_exceeded"}})
    assert result["retryable"] is False
    assert result["kind"] == "CONTEXT_TOO_LARGE"
