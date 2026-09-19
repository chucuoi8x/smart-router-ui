"""E2E-03: Multi-dimensional quota pressure normalization.

Verify that scheduler sees effective pressure as dominated by the most constrained dimension,
not averaged across dimensions.
"""
import pytest
from apps.gateway.routing.scoring import CandidateMetrics, SmartScoreCalculator, ScoringConfig
from apps.gateway.routing.models import ResourceRef, ResourceCandidate


@pytest.mark.asyncio
async def test_multi_dimensional_quota_pressure_dominated_by_tpm():
    """E2E-03: When RPM is at 90% remaining but TPM is at 5% remaining,
    the effective pressure should be dominated by TPM (0.95), not averaged."""
    
    # Setup: candidate with two quota dimensions
    # RPM: 90/100 used = 90% pressure
    # TPM: 95/100 used = 95% pressure (most constrained)
    candidate = ResourceCandidate(
        resource_ref=ResourceRef(
            provider_connection_id="conn_1",
            credential_scope="cred_1",
            model_id="gpt-4"
        ),
        driver_id="generic-openai",
        weight=1.0,
        metadata={
            "quota_resource_ids": ["rpm:conn_1", "tpm:conn_1"]
        }
    )
    
    # Build metrics with multi-dimensional quota
    metrics = CandidateMetrics(
        price_per_million_input=0.0,
        price_per_million_output=0.0,
        rolling_failure_rate=0.0,
        circuit_breaker_state="closed",
        consecutive_failures=0,
        total_attempts=0,
        total_successes=0,
        p50_latency_ms=0.0,
        p99_latency_ms=0.0,
        mean_latency_ms=0.0,
        request_count=0,
        effective_remaining=10,  # min across dimensions (TPM has 5 remaining)
        limit=100,  # max limit
        safety_buffer=0,
        burn_rate_urgency=0.0,
        normalized_pressure=0.95,  # max pressure: TPM at 95%
        expiry_urgency=0.0,
        scarcity=0.0,
        retry_expected_cost=0.0,
        uncertainty=0.0
    )
    
    # Verify normalized_pressure is the max across dimensions
    assert metrics.normalized_pressure == 0.95
    
    # Verify scoring uses normalized_pressure
    config = ScoringConfig(enabled=True)
    calculator = SmartScoreCalculator(config)
    
    score = calculator._score_quota_pressure(metrics)
    
    # Score should reflect high pressure (0.95), not low pressure (0.90)
    # quota_pressure_score = 1.0 - normalized_pressure = 1.0 - 0.95 = 0.05
    assert score == pytest.approx(0.05, abs=0.01)


@pytest.mark.asyncio
async def test_multi_dimensional_quota_pressure_dominated_by_rpm():
    """E2E-03: When TPM is at 20% remaining but RPM is at 95% remaining,
    the effective pressure should be dominated by RPM (0.95)."""
    
    metrics = CandidateMetrics(
        price_per_million_input=0.0,
        price_per_million_output=0.0,
        rolling_failure_rate=0.0,
        circuit_breaker_state="closed",
        consecutive_failures=0,
        total_attempts=0,
        total_successes=0,
        p50_latency_ms=0.0,
        p99_latency_ms=0.0,
        mean_latency_ms=0.0,
        request_count=0,
        effective_remaining=5,  # min across dimensions (RPM has 5 remaining)
        limit=100,  # max limit
        safety_buffer=0,
        burn_rate_urgency=0.0,
        normalized_pressure=0.95,  # max pressure: RPM at 95%
        expiry_urgency=0.0,
        scarcity=0.0,
        retry_expected_cost=0.0,
        uncertainty=0.0
    )
    
    # Verify normalized_pressure is the max across dimensions
    assert metrics.normalized_pressure == 0.95
    
    # Verify scoring uses normalized_pressure
    config = ScoringConfig(enabled=True)
    calculator = SmartScoreCalculator(config)
    
    score = calculator._score_quota_pressure(metrics)
    
    # Score should reflect high pressure (0.95)
    assert score == pytest.approx(0.05, abs=0.01)


@pytest.mark.asyncio
async def test_multi_dimensional_quota_pressure_balanced():
    """E2E-03: When both dimensions are at similar pressure (50%),
    the effective pressure should be 50%."""
    
    metrics = CandidateMetrics(
        price_per_million_input=0.0,
        price_per_million_output=0.0,
        rolling_failure_rate=0.0,
        circuit_breaker_state="closed",
        consecutive_failures=0,
        total_attempts=0,
        total_successes=0,
        p50_latency_ms=0.0,
        p99_latency_ms=0.0,
        mean_latency_ms=0.0,
        request_count=0,
        effective_remaining=50,
        limit=100,
        safety_buffer=0,
        burn_rate_urgency=0.0,
        normalized_pressure=0.50,  # balanced pressure
        expiry_urgency=0.0,
        scarcity=0.0,
        retry_expected_cost=0.0,
        uncertainty=0.0
    )
    
    # Verify normalized_pressure is balanced
    assert metrics.normalized_pressure == 0.50
    
    # Verify scoring uses normalized_pressure
    config = ScoringConfig(enabled=True)
    calculator = SmartScoreCalculator(config)
    
    score = calculator._score_quota_pressure(metrics)
    
    # Score should reflect balanced pressure (0.50)
    assert score == pytest.approx(0.50, abs=0.01)
