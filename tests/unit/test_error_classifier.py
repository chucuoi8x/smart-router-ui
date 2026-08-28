from apps.gateway.providers.error_classifier import classify_provider_error


def test_429_rate_limit_includes_retry_metadata():
    result = classify_provider_error(
        status_code=429,
        body={"error": {"type": "rate_limit_error", "message": "Too many requests"}},
        headers={"Retry-After": "12", "X-RateLimit-Reset-Requests": "1700000000"},
    )

    assert result["kind"] == "RATE_LIMIT"
    assert result["retryable"] is True
    assert result["scope"] == "credential/model/connection"
    assert result["retry_after"] == "12"
    assert result["reset_at"] == "1700000000"
    assert result["provider_error_code"] == "rate_limit_error"


def test_quota_exhausted_is_not_retryable_rate_limit():
    result = classify_provider_error(
        status_code=429,
        body={"error": {"type": "insufficient_quota", "message": "Monthly quota exhausted"}},
        headers={"X-Quota-Reset": "2026-09-01T00:00:00Z"},
    )

    assert result["kind"] == "QUOTA_EXHAUSTED"
    assert result["retryable"] is False
    assert result["scope"] == "account"
    assert result["reset_at"] == "2026-09-01T00:00:00Z"
    assert result["consumption_uncertainty"] == "low"


def test_invalid_request_and_context_are_separate():
    context = classify_provider_error(
        status_code=400,
        body={"error": {"type": "context_length_exceeded", "message": "maximum context exceeded"}},
    )
    invalid = classify_provider_error(status_code=400, body={"error": {"message": "bad schema"}})

    assert context["kind"] == "CONTEXT_TOO_LARGE"
    assert context["retryable"] is False
    assert invalid["kind"] == "INVALID_REQUEST"
    assert invalid["scope"] == "request"


def test_content_policy_and_auth_revoked_are_separate_403s():
    policy = classify_provider_error(
        status_code=403,
        body={"error": {"message": "blocked by policy"}},
    )
    auth = classify_provider_error(status_code=403, body={"error": {"message": "forbidden"}})

    assert policy["kind"] == "CONTENT_POLICY"
    assert policy["scope"] == "request/provider"
    assert auth["kind"] == "AUTH_REVOKED"
    assert auth["scope"] == "credential"


def test_408_is_transient_network_retryable():
    result = classify_provider_error(status_code=408, body={"error": {"message": "request timeout"}})

    assert result["kind"] == "TRANSIENT_NETWORK"
    assert result["retryable"] is True
    assert result["scope"] == "connection"
