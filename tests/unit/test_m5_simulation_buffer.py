"""RED tests Step 106 — simulation mirror safety-buffer reservation."""
from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.routing.models import ResourceCandidate, ResourceRef
from apps.gateway.routing.scoring import ScoringConfig
from apps.gateway.routing.simulation import _dry_run_reservation, simulate_route


def _candidate() -> ResourceCandidate:
    return ResourceCandidate(
        resource_ref=ResourceRef("primary", "primary", "fast"),
        driver_id="generic",
        metadata={"quota_resource_id": "tpm:primary"},
    )


def test_dry_run_includes_safety_buffer():
    result = _dry_run_reservation(_candidate(), estimated_input_tokens=100)
    assert result["estimated_tokens_per_request"] == 100
    # auto-free preset default ratio 0.05 → ceil(100 * 0.05) = 5
    assert result["risk_buffer_ratio"] == 0.05
    assert result["risk_buffer"] == 5
    assert result["required_tokens"] == 105


def test_dry_run_uses_preset_ratio():
    scoring = ScoringConfig.from_dict({"enabled": True, "mode": "active", "preset": "critical"})
    preset = scoring.effective_preset_for_route("any")
    result = _dry_run_reservation(_candidate(), estimated_input_tokens=100, preset=preset)
    # critical preset ratio 0.08 → ceil(8) = 8
    assert result["risk_buffer_ratio"] == 0.08
    assert result["required_tokens"] == 108


def test_simulate_route_reports_required_tokens():
    snapshot = RuntimeConfigSnapshot(
        routes={
            "coding": RouteConfig(
                route_name="coding",
                strategy="priority",
                candidates=[_candidate()],
            )
        }
    )
    scoring = ScoringConfig.from_dict({"enabled": True, "mode": "active", "preset": "coding"})
    result = simulate_route(
        snapshot=snapshot,
        route_name="coding",
        scoring_config=scoring,
        estimated_input_tokens=1000,
    )
    assert result.expected_reservation is not None
    # coding preset ratio 0.05 → required = 1050
    assert result.expected_reservation["risk_buffer_ratio"] == 0.05
    assert result.expected_reservation["required_tokens"] == 1050
