"""RED tests Step 99 — credential invalid/revoked + unsupported protocol là hard filter."""
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


def test_hard_state_rejects_revoked_or_invalid_credential():
    assert hard_state_eligible({"credential_state": "active"}) is True
    assert hard_state_eligible({}) is True  # thiếu data -> fail-open
    assert hard_state_eligible({"credential_state": "revoked"}) is False
    assert hard_state_eligible({"credential_state": "invalid"}) is False
    assert hard_state_eligible({"credential_state": "expired"}) is False
    assert hard_state_eligible({"auth_state": "revoked"}) is False


def test_hard_state_rejects_unsupported_protocol():
    # Request cần protocol được khai báo qua required protocol metadata
    assert hard_state_eligible({"protocols": ["anthropic", "openai"]}) is True
    assert hard_state_eligible({"protocol": "unsupported-xyz"}) is False


def test_router_engine_blocks_revoked_credential_when_scheduler_disabled():
    from apps.gateway.routing.engine import RouterEngine
    snap = RuntimeConfigSnapshot(routes={"chat": RouteConfig(
        route_name="chat", strategy="priority",
        candidates=[
            _resource("ok", {}),
            _resource("revoked", {"credential_state": "revoked"}),
        ],
    )})
    engine = RouterEngine(snap, scoring_config=ScoringConfig())
    models = [c.resource_ref.model_id for c in engine.select_candidates("chat")]
    assert models == ["ok"]


def test_legacy_router_blocks_revoked_credential_when_scheduler_disabled():
    from router import SmartRouter
    config = {
        "routes": {"chat": {"strategy": "priority", "candidates": [
            {"upstream": "p", "model": "ok"},
            {"upstream": "p", "model": "revoked", "credential_state": "revoked"},
        ]}},
        "upstreams": {"p": {"base_url": "https://p", "auth": {"token_env": "P_T"}}},
        "logging": {"level": "CRITICAL"},
    }
    router = SmartRouter(config)
    models = [c.model for c in asyncio.run(router._candidate_order("chat"))]
    assert models == ["ok"]
