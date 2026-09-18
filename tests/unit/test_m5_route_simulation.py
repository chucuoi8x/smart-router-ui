"""RED tests cho Route Simulation dry-run theo README §22.3 / §37.

Không gọi provider; chỉ trả diagnostics: candidates, eligibility per policy,
normalized features, score và selected resource."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.routing.models import ResourceCandidate, ResourceRef
from apps.gateway.routing.presets import PolicyPreset, PolicyConstraints
from apps.gateway.routing.scoring import ScoringConfig


def _snapshot_two_candidates() -> RuntimeConfigSnapshot:
    good = ResourceCandidate(ResourceRef("free-conn", "free-conn", "good"), driver_id="generic", weight=2, metadata={"quality_score": 0.85})
    low_quality = ResourceCandidate(ResourceRef("slow-conn", "slow-conn", "cheap"), driver_id="generic", weight=1, metadata={"quality_score": 0.2})
    return RuntimeConfigSnapshot(routes={"coding": RouteConfig(route_name="coding", strategy="priority", candidates=[good, low_quality])})


def test_simulation_rejects_low_quality_candidate():
    from apps.gateway.routing.simulation import simulate_route

    snapshot = _snapshot_two_candidates()
    scoring = ScoringConfig.from_dict({"enabled": True, "mode": "active", "preset": "review"})
    result = simulate_route(
        snapshot=snapshot,
        route_name="coding",
        scoring_config=scoring,
        estimated_input_tokens=45000,
        max_output_tokens=6000,
        capabilities={"tools": True, "vision": False},
    )
    assert result.route_name == "coding"
    considered = {row["candidate_key"] for row in result.candidates}
    assert "free-conn:free-conn:good" in considered
    low = next(row for row in result.candidates if row["candidate_key"] == "slow-conn:slow-conn:cheap")
    assert low["eligibility"] is False
    assert any("quality" in str(reason).lower() for reason in low["failed_constraints"])  # type: ignore[arg-type]


def test_simulation_does_not_trigger_upstream_call_and_exposes_scores():
    from apps.gateway.routing.simulation import simulate_route

    snapshot = _snapshot_two_candidates()
    scoring = ScoringConfig.from_dict({"enabled": True, "mode": "active", "preset": "coding"})
    result = simulate_route(
        snapshot=snapshot,
        route_name="coding",
        scoring_config=scoring,
        estimated_input_tokens=1000,
        session_id="example-session",
    )
    assert result.selected_resource is not None
    # Diagnostics phải có score và features đã chuẩn hóa mà không phụ thuộc upstream
    chosen = next(row for row in result.candidates if row["candidate_key"] == result.selected_resource)
    assert "composite" in chosen["score"]
    assert "features" in chosen and isinstance(chosen["features"], dict)
    assert chosen["eligibility"] is True


def test_simulation_respects_policy_change():
    from apps.gateway.routing.simulation import simulate_route

    snapshot = _snapshot_two_candidates()
    auto_free = simulate_route(
        snapshot=snapshot,
        route_name="coding",
        scoring_config=ScoringConfig.from_dict({"enabled": True, "mode": "active", "preset": "auto-free"}),
        estimated_input_tokens=1000,
    )
    review = simulate_route(
        snapshot=snapshot,
        route_name="coding",
        scoring_config=ScoringConfig.from_dict({"enabled": True, "mode": "active", "preset": "review"}),
        estimated_input_tokens=1000,
    )
    # Review có min_quality cao hơn nên candidate low_quality vẫn bị loại
    low_af = next(r for r in auto_free.candidates if r["candidate_key"] == "slow-conn:slow-conn:cheap")
    low_rv = next(r for r in review.candidates if r["candidate_key"] == "slow-conn:slow-conn:cheap")
    assert low_af["eligibility"] is False
    assert low_rv["eligibility"] is False
