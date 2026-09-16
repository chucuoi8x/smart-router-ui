"""RED tests Step 98 — hard resource state eligibility không phụ thuộc smart scheduler."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.routing.models import ResourceCandidate, ResourceRef
from apps.gateway.routing.scoring import ScoringConfig


def _resource(model, meta):
    return ResourceCandidate(ResourceRef("p", "p", model), driver_id="generic", metadata=meta)


def test_router_engine_rejects_disabled_and_open_circuit_when_scheduler_disabled():
    from apps.gateway.routing.engine import RouterEngine
    snap = RuntimeConfigSnapshot(routes={"chat": RouteConfig(
        route_name="chat", strategy="priority",
        candidates=[
            _resource("ok", {}),
            _resource("disabled", {"enabled": False}),
            _resource("circuit", {"circuit_state": "open"}),
            _resource("quality-low", {"quality_score": 0.0}),
        ],
    )})
    engine = RouterEngine(snap, scoring_config=ScoringConfig())
    models = [c.resource_ref.model_id for c in engine.select_candidates("chat")]
    assert models == ["ok", "quality-low"]  # quality policy remains disabled


def test_legacy_router_rejects_disabled_and_open_circuit_when_scheduler_disabled():
    from router import SmartRouter
    config = {
        "routes": {"chat": {"strategy": "priority", "candidates": [
            {"upstream": "p", "model": "ok"},
            {"upstream": "p", "model": "disabled", "enabled": False},
            {"upstream": "p", "model": "circuit", "circuit_state": "open"},
        ]}},
        "upstreams": {"p": {"base_url": "https://p", "auth": {"token_env": "P_T"}}},
        "logging": {"level": "CRITICAL"},
    }
    router = SmartRouter(config)
    models = [c.model for c in asyncio.run(router._candidate_order("chat"))]
    assert models == ["ok"]
