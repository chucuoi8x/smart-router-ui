"""RED/GREEN tests cho session affinity M5 §20."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.routing.scoring import CandidateMetrics, ScoringConfig, SmartScoreCalculator


def test_session_affinity_bonus_changes_order():
    cfg = ScoringConfig.from_dict({
        "enabled": True,
        "mode": "active",
        "weights": {"session_affinity_factor": 1.0},
    })
    calc = SmartScoreCalculator(config=cfg)
    calc.remember_affinity("session-1", "b:m2")
    metrics = {"a:m1": CandidateMetrics(), "b:m2": CandidateMetrics()}

    scored = calc.compute_scores(
        candidates=["a", "b"],
        candidate_keys=["a:m1", "b:m2"],
        metrics_by_key=metrics,
        conversation_thread="session-1",
    )

    assert [candidate for candidate, _ in scored] == ["b", "a"]


async def test_router_candidate_order_uses_session_affinity():
    from router import SmartRouter

    router = SmartRouter({
        "smart_scheduler": {
            "enabled": True,
            "mode": "active",
            "weights": {"session_affinity_factor": 1.0},
        },
        "routes": {
            "coding": {
                "strategy": "priority",
                "candidates": [
                    {"upstream": "a", "model": "m1"},
                    {"upstream": "b", "model": "m2"},
                ],
                "fallback": [],
            },
        },
    })
    router._ensure_scoring()
    router._score_calculator.remember_affinity("session-1", "b:m2")

    ordered = await router._candidate_order("coding", conversation_thread="session-1")

    assert ordered[0].key == "b:m2"


def test_session_hint_prefers_explicit_header_over_body():
    from router import SmartRouter

    router = SmartRouter({"routes": {}})
    assert router._conversation_thread_hint(
        {"session_id": "body-session"},
        {"x-session-id": "header-session"},
    ) == "header-session"


def test_session_hint_accepts_body_conversation_id_without_persisting_content():
    from router import SmartRouter

    router = SmartRouter({"routes": {}})
    assert router._conversation_thread_hint(
        {"conversation_id": "conv-123", "messages": [{"content": "secret prompt"}]},
        {},
    ) == "conv-123"


def test_router_engine_uses_session_affinity():
    from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
    from apps.gateway.routing.engine import RouterEngine
    from apps.gateway.routing.models import ResourceCandidate, ResourceRef

    first = ResourceCandidate(ResourceRef("a", "a", "m1"), driver_id="generic")
    preferred = ResourceCandidate(ResourceRef("b", "b", "m2"), driver_id="generic")
    snapshot = RuntimeConfigSnapshot(
        routes={"chat": RouteConfig(route_name="chat", strategy="priority", candidates=[first, preferred])}
    )
    scoring = ScoringConfig.from_dict({
        "enabled": True,
        "mode": "active",
        "weights": {"session_affinity_factor": 1.0},
    })
    engine = RouterEngine(snapshot, scoring_config=scoring)
    engine._score_calculator.remember_affinity("session-1", "b:b:m2")

    ordered = engine.select_candidates("chat", conversation_thread="session-1")

    assert [candidate.resource_ref.model_id for candidate in ordered] == ["m2", "m1"]
