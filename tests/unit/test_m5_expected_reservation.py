"""Tests cho expected reservation diagnostics trong route simulation (Step 95 / README §22.3)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.routing.models import ResourceCandidate, ResourceRef
from apps.gateway.routing.scoring import ScoringConfig
from apps.gateway.routing.simulation import simulate_route


def test_simulation_exposes_expected_reservation_per_candidate_and_top_level():
    cand1 = ResourceCandidate(
        ResourceRef("primary", "primary", "primary-model"),
        driver_id="generic",
        weight=1,
        metadata={"quota_resource_ids": ["account:primary", "model:primary-model"], "quality_score": 0.9},
    )
    cand2 = ResourceCandidate(
        ResourceRef("backup", "backup", "backup-model"),
        driver_id="generic",
        weight=1,
        metadata={"quality_score": 0.8},
    )
    snapshot = RuntimeConfigSnapshot(
        routes={"coding": RouteConfig(route_name="coding", strategy="priority", candidates=[cand1, cand2])}
    )
    scoring = ScoringConfig.from_dict({"enabled": True, "mode": "active", "preset": "coding"})

    result = simulate_route(
        snapshot=snapshot,
        route_name="coding",
        scoring_config=scoring,
        estimated_input_tokens=45000,
        max_output_tokens=6000,
    )

    # 1. Top-level expected_reservation
    assert result.expected_reservation is not None
    assert isinstance(result.expected_reservation, dict)
    assert result.expected_reservation["estimated_tokens_per_request"] == 45000
    assert "account:primary" in result.expected_reservation["resource_ids"]

    # 2. Per-candidate expected_reservation
    c1 = next(r for r in result.candidates if r["candidate_key"] == "primary:primary:primary-model")
    assert "expected_reservation" in c1
    assert c1["expected_reservation"]["resource_ids"] == ["account:primary", "model:primary-model"]
    assert c1["expected_reservation"]["estimated_tokens_per_request"] == 45000

    c2 = next(r for r in result.candidates if r["candidate_key"] == "backup:backup:backup-model")
    assert "model:backup-model" in c2["expected_reservation"]["resource_ids"]
