from apps.gateway.providers.error_classifier import classify_provider_error


def test_classification_exposes_one_explicit_scope():
    result = classify_provider_error(status_code=429, body={"error": {"message": "rate limit"}}, headers={})
    assert result["failure_scope"].value == "credential"
    assert result["failure_scope"].value in {"request", "credential", "model", "connection", "provider", "account", "attempt"}
