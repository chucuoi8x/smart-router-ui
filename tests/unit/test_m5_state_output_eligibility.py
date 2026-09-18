"""RED tests cho Step 97 — disabled/deprecated/output-limit eligibility (README §18.2)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.routing.models import ResourceCandidate, ResourceRef
from apps.gateway.routing.presets import PolicyConstraints, filter_candidates_for_policy
from apps.gateway.routing.simulation import simulate_route


def _cand(mid, meta):
    return ResourceCandidate(ResourceRef("conn", "conn", mid), driver_id="generic", weight=1, metadata=meta)


def test_filter_rejects_disabled_resource():
    on = _cand("m-on", {"enabled": True})
    off = _cand("m-off", {"enabled": False})
    unconstrained = _cand("m-missing", {})
    out = [c.resource_ref.model_id for c in filter_candidates_for_policy([on, off, unconstrained], PolicyConstraints())]
    assert "m-on" in out
    assert "m-off" not in out
    assert "m-missing" in out  # thiếu data -> fail-open


def test_filter_rejects_deprecated_or_unavailable_model():
    fresh = _cand("m-fresh", {"model_state": "verified"})
    dep = _cand("m-dep", {"model_state": "deprecated"})
    hidden = _cand("m-hidden", {"model_state": "hidden"})
    offmeta = _cand("m-off", {"deprecated": True})
    out = [c.resource_ref.model_id for c in filter_candidates_for_policy([fresh, dep, hidden, offmeta], PolicyConstraints())]
    assert "m-fresh" in out
    assert "m-dep" not in out
    assert "m-hidden" not in out
    assert "m-off" not in out


def test_filter_rejects_when_output_limit_too_small():
    big = _cand("m-big", {"max_output_tokens": 16000})
    small = _cand("m-small", {"max_output_tokens": 1024})
    unknown = _cand("m-unknown", {})
    cons = PolicyConstraints()
    # Không có yêu cầu -> không lọc
    assert len(filter_candidates_for_policy([big, small, unknown], cons)) == 3
    # Yêu cầu output 4000 -> loại m-small, giữ unknown (fail-open)
    out = [c.resource_ref.model_id for c in filter_candidates_for_policy(
        [big, small, unknown], cons, required_capabilities={"min_output_tokens": 4000})]
    assert "m-big" in out
    assert "m-small" not in out
    assert "m-unknown" in out


def test_simulation_reports_output_and_state_failures():
    ok = _cand("m-ok", {"max_output_tokens": 8000, "model_state": "verified"})
    dep = _cand("m-dep", {"model_state": "deprecated"})
    small = _cand("m-small", {"max_output_tokens": 512, "model_state": "verified"})
    snap = RuntimeConfigSnapshot(routes={"r": RouteConfig(route_name="r", strategy="priority", candidates=[ok, dep, small])})
    res = simulate_route(snapshot=snap, route_name="r", capabilities={"min_output_tokens": 4000})
    rows = {r["model_id"]: r for r in res.candidates}
    assert rows["m-ok"]["eligibility"] is True
    assert rows["m-dep"]["eligibility"] is False
    assert any("deprecat" in f or "unavailable" in f for f in rows["m-dep"]["failed_constraints"])
    assert rows["m-small"]["eligibility"] is False
    assert any("output" in f for f in rows["m-small"]["failed_constraints"])
    assert res.selected_resource == "conn:conn:m-ok"
