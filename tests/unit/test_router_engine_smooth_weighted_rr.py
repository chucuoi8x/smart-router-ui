from __future__ import annotations

from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.routing.engine import RouterEngine
from apps.gateway.routing.models import ResourceCandidate, ResourceRef


def _candidate(connection: str, model: str, weight: float) -> ResourceCandidate:
    return ResourceCandidate(ResourceRef(connection, connection, model), driver_id="generic", weight=weight)


def test_router_engine_smooth_weighted_rr_keeps_fallback_last_and_cycles_primary():
    primary_a = _candidate("proxypal", "a", 4)
    primary_b = _candidate("proxypal", "b", 3)
    fallback = _candidate("aibox", "z", 1)
    snapshot = RuntimeConfigSnapshot(
        routes={
            "r": RouteConfig(
                route_name="r",
                strategy="smooth_weighted_rr",
                candidates=[primary_a, primary_b],
                fallback=[fallback],
            )
        }
    )
    engine = RouterEngine(snapshot)

    first_candidates = engine.select_candidates("r")
    second_candidates = engine.select_candidates("r")

    assert first_candidates[-1].resource_ref == fallback.resource_ref
    assert second_candidates[-1].resource_ref == fallback.resource_ref
    assert {first_candidates[0].resource_ref, second_candidates[0].resource_ref} == {
        primary_a.resource_ref,
        primary_b.resource_ref,
    }
    assert {c.resource_ref for c in first_candidates[:2]} == {primary_a.resource_ref, primary_b.resource_ref}
    assert {c.resource_ref for c in second_candidates[:2]} == {primary_a.resource_ref, primary_b.resource_ref}
