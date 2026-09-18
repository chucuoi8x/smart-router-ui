from dataclasses import FrozenInstanceError

import pytest

from apps.gateway.config.compiler import LegacyConfigCompiler
from apps.gateway.config.snapshot import ConnectionConfig, RouteConfig, RuntimeConfigSnapshot
from apps.gateway.routing.models import ResourceCandidate, ResourceRef


def _candidate(metadata=None):
    return ResourceCandidate(
        ResourceRef("connection", "credential", "model"),
        driver_id="anthropic-compatible",
        metadata=metadata or {},
    )


def test_snapshot_dataclasses_reject_attribute_assignment():
    connection = ConnectionConfig("connection", "https://example.test", "bearer", "TOKEN")
    route = RouteConfig("chat", "priority")
    snapshot = RuntimeConfigSnapshot(
        connections={"connection": connection},
        routes={"chat": route},
    )

    with pytest.raises(FrozenInstanceError):
        connection.base_url = "https://changed.test"
    with pytest.raises(FrozenInstanceError):
        route.strategy = "changed"
    with pytest.raises(FrozenInstanceError):
        snapshot.routes = {}


def test_snapshot_deep_freezes_mappings_sequences_and_candidate_metadata():
    nested_metadata = {
        "quota_resource_ids": ["account:one", "model:one"],
        "capabilities": {"input": ["text", {"kind": "image"}]},
    }
    candidate = _candidate(nested_metadata)
    connections = {
        "connection": ConnectionConfig("connection", "https://example.test", "bearer", "TOKEN")
    }
    candidates = [candidate]
    fallback = [_candidate({"labels": ["fallback"]})]
    routes = {
        "chat": RouteConfig(
            route_name="chat",
            strategy="priority",
            candidates=candidates,
            fallback=fallback,
        )
    }

    snapshot = RuntimeConfigSnapshot(connections=connections, routes=routes)
    frozen_route = snapshot.routes["chat"]
    frozen_metadata = frozen_route.candidates[0].metadata

    connections.clear()
    routes.clear()
    candidates.clear()
    fallback.clear()
    nested_metadata["quota_resource_ids"].append("late")
    nested_metadata["capabilities"]["input"][1]["kind"] = "changed"

    assert set(snapshot.connections) == {"connection"}
    assert set(snapshot.routes) == {"chat"}
    assert len(frozen_route.candidates) == 1
    assert len(frozen_route.fallback) == 1
    assert frozen_metadata["quota_resource_ids"] == ["account:one", "model:one"]
    assert frozen_metadata["capabilities"]["input"][1]["kind"] == "image"

    with pytest.raises(TypeError):
        snapshot.connections["other"] = snapshot.connections["connection"]
    with pytest.raises(TypeError):
        snapshot.routes["other"] = frozen_route
    with pytest.raises(AttributeError):
        frozen_route.candidates.append(candidate)
    with pytest.raises(TypeError):
        frozen_metadata["new"] = "value"
    with pytest.raises(AttributeError):
        frozen_metadata["quota_resource_ids"].append("new")
    with pytest.raises(TypeError):
        frozen_metadata["capabilities"]["input"][1]["kind"] = "new"


def test_compiler_returns_deeply_immutable_snapshot_without_aliasing_input():
    config = {
        "upstreams": {
            "primary": {
                "base_url": "https://primary.test",
                "auth": {"mode": "bearer", "token_env": "PRIMARY_TOKEN"},
            }
        },
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [
                    {
                        "upstream": "primary",
                        "model": "model-one",
                        "quota_resource_ids": ["account:primary", "model:one"],
                        "capabilities": ["chat", {"modalities": ["text", "image"]}],
                    }
                ],
            }
        },
    }

    snapshot = LegacyConfigCompiler().compile_dict(config)
    metadata = snapshot.routes["chat"].candidates[0].metadata
    config["routes"]["chat"]["candidates"][0]["quota_resource_ids"].append("late")
    config["routes"]["chat"]["candidates"][0]["capabilities"][1]["modalities"].append("audio")

    assert metadata["quota_resource_ids"] == ["account:primary", "model:one"]
    assert metadata["capabilities"] == ["chat", {"modalities": ["text", "image"]}]
    with pytest.raises(AttributeError):
        metadata["capabilities"][1]["modalities"].append("audio")
