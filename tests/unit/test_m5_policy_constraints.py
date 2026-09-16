"""Regression tests cho M5 policy constraint enforcement."""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.gateway.routing.presets import (
    PolicyConstraints,
    PolicyPreset,
    filter_candidates_for_policy,
)


@dataclass(frozen=True)
class FakeCandidate:
    name: str
    metadata: dict = field(default_factory=dict)


def _preset(**overrides) -> PolicyPreset:
    return PolicyPreset(
        name="test",
        constraints=PolicyConstraints(**overrides),
    )


def test_rejects_candidate_below_policy_quality_floor():
    candidates = [
        FakeCandidate("low", {"quality_score": 0.64}),
        FakeCandidate("good", {"quality_score": 0.65}),
    ]

    accepted = filter_candidates_for_policy(candidates, _preset(min_quality=0.65))

    assert [candidate.name for candidate in accepted] == ["good"]


def test_rejects_paid_fallback_when_policy_disallows_it():
    candidates = [
        FakeCandidate("free", {"is_paid": False}),
        FakeCandidate("paid", {"is_paid": True, "expected_cost_per_request": 0.02}),
    ]

    accepted = filter_candidates_for_policy(
        candidates,
        _preset(allow_paid_fallback=False),
        is_fallback=True,
    )

    assert [candidate.name for candidate in accepted] == ["free"]


def test_rejects_paid_fallback_without_known_cost_or_over_ceiling():
    candidates = [
        FakeCandidate("unknown", {"is_paid": True}),
        FakeCandidate("over", {"is_paid": True, "expected_cost_per_request": 0.11}),
        FakeCandidate("within", {"is_paid": True, "expected_cost_per_request": 0.10}),
    ]

    accepted = filter_candidates_for_policy(
        candidates,
        _preset(allow_paid_fallback=True, max_expected_cost_per_request=0.10),
        is_fallback=True,
    )

    assert [candidate.name for candidate in accepted] == ["within"]


def test_rejects_known_quota_headroom_below_policy_floor():
    candidates = [
        FakeCandidate("low", {"quota_remaining_by_resource": {"model:low": 2}, "quota_limit_by_resource": {"model:low": 100}}),
        FakeCandidate("enough", {"quota_remaining_by_resource": {"model:enough": 3}, "quota_limit_by_resource": {"model:enough": 100}}),
        FakeCandidate("unknown", {}),
    ]

    accepted = filter_candidates_for_policy(candidates, _preset(min_quota_headroom=0.03))

    assert [candidate.name for candidate in accepted] == ["enough", "unknown"]
