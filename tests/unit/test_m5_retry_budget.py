"""RED tests cho M5 retry budget theo README §21 / Step 94."""
from __future__ import annotations

from apps.gateway.routing.presets import PolicyPreset, RetryPolicy


def test_retry_policy_supports_latency_and_error_budget():
    policy = RetryPolicy(
        max_attempts=2,
        max_extra_input_tokens=60000,
        max_extra_latency_ms=5000,
        retryable_errors=("RATE_LIMIT", "OVERLOADED"),
    )
    assert policy.max_extra_latency_ms == 5000
    assert policy.retryable_errors == ("RATE_LIMIT", "OVERLOADED")


def test_retry_budget_allows_retry_within_attempt_token_latency_limits():
    from apps.gateway.routing.retry import RetryBudget

    budget = RetryBudget(
        RetryPolicy(
            max_attempts=3,
            max_extra_input_tokens=1000,
            max_extra_latency_ms=500,
            retryable_errors=("RATE_LIMIT",),
        ),
        estimated_input_tokens=400,
    )
    assert budget.can_retry("RATE_LIMIT", extra_input_tokens=400, extra_latency_ms=200)
    budget.record_retry(extra_input_tokens=400, extra_latency_ms=200)
    assert budget.can_retry("RATE_LIMIT", extra_input_tokens=200, extra_latency_ms=100)


def test_retry_budget_blocks_non_retryable_and_over_budget_attempts():
    from apps.gateway.routing.retry import RetryBudget

    budget = RetryBudget(
        RetryPolicy(
            max_attempts=2,
            max_extra_input_tokens=1000,
            max_extra_latency_ms=500,
            retryable_errors=("RATE_LIMIT",),
        ),
        estimated_input_tokens=600,
    )
    assert not budget.can_retry("INVALID_REQUEST", extra_input_tokens=1, extra_latency_ms=1)
    # extra vượt max_extra_input_tokens
    assert not budget.can_retry("RATE_LIMIT", extra_input_tokens=1100, extra_latency_ms=1)
    assert not budget.can_retry("RATE_LIMIT", extra_input_tokens=1, extra_latency_ms=600)


def test_retry_budget_respects_max_attempts():
    from apps.gateway.routing.retry import RetryBudget

    budget = RetryBudget(
        RetryPolicy(max_attempts=2, retryable_errors=("OVERLOADED",)),
        estimated_input_tokens=0,
    )
    assert budget.can_retry("OVERLOADED", extra_input_tokens=0, extra_latency_ms=0)
    budget.record_retry(extra_input_tokens=0, extra_latency_ms=0)
    assert not budget.can_retry("OVERLOADED", extra_input_tokens=0, extra_latency_ms=0)
