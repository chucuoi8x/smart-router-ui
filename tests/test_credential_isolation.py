"""P0-06: canonical credential identity.

Scheduling identity = connection + credential + model (plan §3.3).  Two
credentials for the same connection+model are two different resources: circuit
breaker, latency, failure rate and session affinity must never be shared
between sibling credentials.
"""

import time

import pytest

from router import Candidate, SmartRouter


def _router() -> SmartRouter:
    return SmartRouter({"routes": {}, "logging": {"level": "CRITICAL"}})


def _rate_limit_classification() -> dict:
    return {
        "kind": "RATE_LIMIT",
        "scope": "credential/model/connection",
        "retry_after": "60",
        "reset_at": None,
        "consumption_uncertainty": "unknown",
    }


def _pair() -> tuple[Candidate, Candidate]:
    # Same connection + model, different credentials.
    c1 = Candidate(upstream="conn1", model="gpt-4", resource_key="conn1:credA:gpt-4")
    c2 = Candidate(upstream="conn1", model="gpt-4", resource_key="conn1:credB:gpt-4")
    return c1, c2


def test_candidate_key_includes_credential_from_resource_key():
    c1, c2 = _pair()
    assert c1.key == "conn1:credA:gpt-4"
    assert c2.key == "conn1:credB:gpt-4"
    assert c1.key != c2.key


def test_candidate_key_includes_credential_from_metadata():
    c1 = Candidate(upstream="conn1", model="gpt-4", metadata={"credential_id": "credA"})
    c2 = Candidate(upstream="conn1", model="gpt-4", metadata={"credential_id": "credB"})
    assert c1.key == "conn1:credA:gpt-4"
    assert c2.key == "conn1:credB:gpt-4"
    assert c1.key != c2.key


def test_candidate_key_legacy_without_credential_is_unchanged():
    c = Candidate(upstream="proxypal", model="gpt-4")
    assert c.key == "proxypal:gpt-4"


def test_config_candidate_carries_credential_id_into_metadata():
    router = SmartRouter({"routes": {}, "logging": {"level": "CRITICAL"}})
    (c,) = router._parse_candidates(
        [{"upstream": "conn1", "model": "gpt-4", "credential_id": "credA"}]
    )
    assert c.key == "conn1:credA:gpt-4"


@pytest.mark.asyncio
async def test_credential_identity_isolation():
    """2 credentials on same connection+model keep separate runtime state."""
    router = _router()
    router._ensure_scoring()
    assert router._score_calculator is not None
    c1, c2 = _pair()

    # --- circuit breaker isolation ---
    await router._record_failure(c1, 429, "rate limited", _rate_limit_classification())
    assert c1.key in router.circuits
    assert router.circuits[c1.key].consecutive_failures == 1
    assert c2.key not in router.circuits
    assert router._is_available(c2) is True
    assert router._is_available(c1) is False

    # --- failure-rate isolation ---
    ft = router._failure_tracker
    ft.record(c1.key, False)
    ft.record(c2.key, True)
    rate1, total1, _ = ft.failure_rate(c1.key)
    rate2, total2, _ = ft.failure_rate(c2.key)
    assert total1 == 2 and rate1 > 0.4
    assert total2 == 1 and rate2 == 0.0

    # --- latency isolation ---
    router._latency_tracker.record(c1.key, 500.0)
    p50_1, _, _ = router._latency_tracker.percentiles(c1.key)
    p50_2, _, mean2 = router._latency_tracker.percentiles(c2.key)
    assert p50_1 == 500.0
    assert p50_2 == 0.0 and mean2 == 0.0

    # --- session affinity isolation ---
    router._remember_session_affinity("thread-1", c1)
    router._remember_session_affinity("thread-2", c2)
    assert router._session_store.get("thread-1") == "conn1:credA:gpt-4"
    assert router._session_store.get("thread-2") == "conn1:credB:gpt-4"

    # --- success on one credential must not clear the other's circuit ---
    await router._record_success(c2, 200)
    state1 = router.circuits[c1.key]
    assert state1.consecutive_failures == 1
    assert state1.cooldown_until > time.monotonic()


@pytest.mark.asyncio
async def test_engine_circuit_trip_is_credential_scoped():
    """Tripping via the router engine repository must use the credential too."""
    router = _router()
    assert router.router_engine is not None
    c1, c2 = _pair()
    router._trip_router_engine_circuit(c1, 30.0)
    repo = router.router_engine.circuit_repository
    assert repo.is_available(c2) is True
    assert repo.is_available(c1) is False
