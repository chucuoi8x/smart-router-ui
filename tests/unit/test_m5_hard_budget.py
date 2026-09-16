"""RED tests Step 100 — project budget exhausted là hard filter luôn bật (README §18.2)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.routing.models import ResourceCandidate, ResourceRef
from apps.gateway.routing.presets import hard_state_eligible
from apps.gateway.routing.scoring import ScoringConfig


def _resource(model, meta):
    return ResourceCandidate(ResourceRef("p", "p", model), driver_id="generic", metadata=meta)


def test_hard_state_rejects_exhausted_budget():
    assert hard_state_eligible({"project_budget_state": "ok"}) is True
    assert hard_state_eligible({}) is True  # thiếu data -> fail-open
    assert hard_state_eligible({"project_budget_state": "exhausted"}) is False
    assert hard_state_eligible({"budget_state": "exhausted"}) is False
    assert hard_state_eligible({"project_budget_exhausted": True}) is False


def test_router_engine_blocks_exhausted_budget_when_scheduler_disabled():
    from apps.gateway.routing.engine import RouterEngine
    snap = RuntimeConfigSnapshot(routes={"chat": RouteConfig(
        route_name="chat", strategy="priority",
        candidates=[
            _resource("ok", {}),
            _resource("broke", {"project_budget_state": "exhausted"}),
        ],
    )})
    engine = RouterEngine(snap, scoring_config=ScoringConfig())  # scheduler tắt
    models = [c.resource_ref.model_id for c in engine.select_candidates("chat")]
    assert models == ["ok"]


def test_legacy_router_blocks_exhausted_budget_when_scheduler_disabled():
    from router import SmartRouter
    config = {
        "routes": {"chat": {"strategy": "priority", "candidates": [
            {"upstream": "p", "model": "ok"},
            {"upstream": "p", "model": "broke", "project_budget_state": "exhausted"},
        ]}},
        "upstreams": {"p": {"base_url": "https://p", "auth": {"token_env": "P_T"}}},
        "logging": {"level": "CRITICAL"},
    }
    router = SmartRouter(config)
    models = [c.model for c in asyncio.run(router._candidate_order("chat"))]
    assert models == ["ok"]


def test_simulation_marks_budget_exhausted_ineligible():
    from apps.gateway.routing.simulation import simulate_route
    snap = RuntimeConfigSnapshot(routes={"chat": RouteConfig(
        route_name="chat", strategy="priority",
        candidates=[_resource("ok", {}), _resource("broke", {"project_budget_state": "exhausted"})],
    )})
    result = simulate_route(snap, "chat", scoring_config=ScoringConfig())
    broke = next(r for r in result.candidates if r["model_id"] == "broke")
    assert broke["eligibility"] is False
    assert any("budget" in f for f in broke["failed_constraints"])
