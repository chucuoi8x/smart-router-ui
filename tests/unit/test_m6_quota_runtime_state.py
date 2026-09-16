"""RED tests Step 150 — AC-07 rate-limit vs quota-exhaustion runtime states."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_rate_limit_runtime_is_short_term_retryable():
    from apps.gateway.quota.runtime import describe_runtime_state

    state = describe_runtime_state("RATE_LIMIT", reset_at="42", retry_after="42")

    assert state["state"] == "rate_limited"
    assert state["retryable"] is True
    assert state["is_long_term"] is False
    assert state["reset_at"] == "42"
    assert state["kind"] == "RATE_LIMIT"


def test_quota_exhausted_runtime_is_long_term_not_retryable():
    from apps.gateway.quota.runtime import describe_runtime_state

    state = describe_runtime_state("QUOTA_EXHAUSTED", reset_at="2026-09-01T00:00:00Z")

    assert state["state"] == "quota_exhausted"
    assert state["retryable"] is False
    assert state["is_long_term"] is True
    assert state["reset_at"] == "2026-09-01T00:00:00Z"
    assert state["kind"] == "QUOTA_EXHAUSTED"


def test_quota_exhausted_cooldown_longer_than_rate_limit():
    from router import SmartRouter

    router = SmartRouter({"routes": {}, "logging": {"level": "CRITICAL"}})
    rate = router._default_cooldown_seconds("RATE_LIMIT", 1)
    quota = router._default_cooldown_seconds("QUOTA_EXHAUSTED", 1)
    assert quota is not None and rate is not None
    assert quota > rate
    assert quota == 3600.0
    assert rate == 15.0


def test_runtime_states_are_distinct_in_failure_decision():
    from router import SmartRouter

    router = SmartRouter({"routes": {}, "logging": {"level": "CRITICAL"}})
    rate_decision = router._failure_runtime_decision(
        429,
        {"kind": "RATE_LIMIT", "scope": "credential/model/connection", "retry_after": "10", "reset_at": "10"},
        has_next=True,
    )
    quota_decision = router._failure_runtime_decision(
        429,
        {"kind": "QUOTA_EXHAUSTED", "scope": "account", "retry_after": None, "reset_at": "2026-09-01T00:00:00Z"},
        has_next=True,
    )
    assert rate_decision.kind == "RATE_LIMIT"
    assert quota_decision.kind == "QUOTA_EXHAUSTED"
    assert rate_decision.kind != quota_decision.kind
    # RATE_LIMIT is scored as failure, QUOTA_EXHAUSTED is suppressed for scoring
    assert rate_decision.record_scoring_failure is True
    assert quota_decision.record_scoring_failure is False
    # QUOTA_EXHAUSTED should reconcile quota observation, RATE_LIMIT should not
    assert quota_decision.quota_observation is True
    assert rate_decision.quota_observation is False
