"""RED tests cho concurrency capacity eligibility — Step 96.

Yêu cầu: Router phải loại candidate khi concurrency đã bão hòa,
không chỉ dựa quota/capability. Fail-open khi thiếu telemetry.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.routing.models import ResourceCandidate, ResourceRef
from apps.gateway.routing.presets import PolicyConstraints, filter_candidates_for_policy, PolicyPreset
from apps.gateway.routing.scoring import ScoringConfig
from apps.gateway.routing.simulation import simulate_route


def _cand(mid, meta):
    return ResourceCandidate(ResourceRef("conn", "conn", mid), driver_id="generic", weight=1, metadata=meta)

def test_filter_rejects_when_concurrency_exhausted():
    c_ok = _cand("m-ok", {"concurrency_used": 2, "concurrency_limit": 10, "quality_score": 0.9})
    c_full = _cand("m-full", {"concurrency_used": 10, "concurrency_limit": 10, "quality_score": 0.9})
    c_over = _cand("m-over", {"concurrency_used": 11, "concurrency_limit": 10, "quality_score": 0.9})
    constraints = PolicyConstraints(min_quality=0.0)
    # concurrency exhausted -> loại
    out = filter_candidates_for_policy([c_ok, c_full, c_over], constraints)
    keys = [x.resource_ref.model_id for x in out]
    assert "m-ok" in keys
    assert "m-full" not in keys
    assert "m-over" not in keys

def test_filter_fail_open_when_no_concurrency_telemetry():
    c = _cand("m-no-telemetry", {"quality_score": 0.9})
    constraints = PolicyConstraints(min_quality=0.0)
    out = filter_candidates_for_policy([c], constraints)
    assert len(out) == 1

def test_simulation_marks_concurrency_ineligible():
    c_ok = _cand("m-ok", {"concurrency_used": 1, "concurrency_limit": 5, "quality_score": 0.9})
    c_busy = _cand("m-busy", {"concurrency_used": 5, "concurrency_limit": 5, "quality_score": 0.9})
    snap = RuntimeConfigSnapshot(routes={"r": RouteConfig(route_name="r", strategy="priority", candidates=[c_ok, c_busy])})
    res = simulate_route(snapshot=snap, route_name="r", scoring_config=None)
    row_ok = next(r for r in res.candidates if r["model_id"] == "m-ok")
    row_busy = next(r for r in res.candidates if r["model_id"] == "m-busy")
    assert row_ok["eligibility"] is True
    assert row_busy["eligibility"] is False
    assert any("concurrency" in f for f in row_busy["failed_constraints"])
