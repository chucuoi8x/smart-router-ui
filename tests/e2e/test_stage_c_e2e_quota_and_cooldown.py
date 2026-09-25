"""Stage C E2E-04/E2E-05: atomic Redis reservation and cross-instance cooldown.

E2E-04 drives the production async Redis quota authority (Lua scripts) so
concurrent reservations are proven atomic on the authority itself, not on the
in-memory fallback.

E2E-05 runs two RouterEngine instances against one shared async Redis circuit
authority: a credential throttled through instance A must disappear from
instance B's schedule, while a sibling credential stays schedulable.
"""
from __future__ import annotations

import asyncio
import time

import pytest

fakeredis = pytest.importorskip("fakeredis")

from apps.gateway.config.snapshot import RouteConfig, RuntimeConfigSnapshot
from apps.gateway.quota import redis_backend as rb
from apps.gateway.quota.redis_backend import RedisQuotaStore
from apps.gateway.quota.reservations import QuotaReservationRequest, QuotaResource
from apps.gateway.routing.engine import (
    CircuitRepository,
    InMemoryCircuitRepository,
    RedisCircuitRepository,
    RouterEngine,
)
from apps.gateway.routing.models import ResourceCandidate, ResourceRef


def _redis_store(client) -> RedisQuotaStore:
    """RedisQuotaStore bound to a fakeredis client, no server needed."""
    store = RedisQuotaStore.__new__(RedisQuotaStore)
    store._r = client
    store._ttl = 60
    store._max_parent_depth = rb._MAX_PARENT_DEPTH
    store._sha_map = {}
    store._script_bodies = {
        "check_many": rb.LUA_CHECK_MANY,
        "reserve_many": rb.LUA_RESERVE_MANY,
        "reconcile": rb.LUA_RECONCILE,
        "release": rb.LUA_RELEASE,
    }
    return store


@pytest.mark.asyncio
async def test_e2e04_concurrent_atomic_reservation_has_no_oversubscription():
    """Plan E2E-04: 40000+40000+35000 against 100000 => exactly two accepted.

    Reservation goes through RedisQuotaStore.reserve_many (Lua) on the shared
    authority, so no check-then-set race is possible.
    """
    store = _redis_store(fakeredis.aioredis.FakeRedis(decode_responses=True))
    await store.add_resource(
        QuotaResource("tokens:e2e04", "credential", "tokens", 100000, 60)
    )

    async def reserve(reservation_id: str, amount: int):
        return await store.reserve_many(
            reservation_id=reservation_id,
            requests=[QuotaReservationRequest("tokens:e2e04", amount)],
        )

    r1, r2, r3 = await asyncio.gather(
        reserve("r1", 40000),
        reserve("r2", 40000),
        reserve("r3", 35000),
    )
    results = {"r1": r1, "r2": r2, "r3": r3}

    accepted = [name for name, res in results.items() if res.accepted]
    rejected = [name for name, res in results.items() if not res.accepted]
    assert len(accepted) == 2, f"expected exactly two accepted, got {accepted}"
    assert len(rejected) == 1, f"expected exactly one rejected, got {rejected}"
    assert rejected[0] == "r3", "the request that cannot fit must be rejected"
    assert results[rejected[0]].reason == "quota_exceeded"

    state = await store.snapshot("tokens:e2e04")
    assert state.used == 80000, "exactly the two winning reservations may be held"
    assert state.used <= state.limit, "oversubscription is not permitted"


def _circuit_snapshot() -> RuntimeConfigSnapshot:
    route = RouteConfig(
        route_name="chat",
        candidates=(
            ResourceCandidate(ResourceRef("conn", "cred-a", "model"), "generic-openai"),
            ResourceCandidate(ResourceRef("conn", "cred-b", "model"), "generic-openai"),
        ),
        fallback=(),
        strategy="priority",
    )
    return RuntimeConfigSnapshot(routes={"chat": route})


@pytest.mark.asyncio
async def test_e2e05_cross_instance_cooldown_propagates_over_shared_redis():
    """Plan E2E-05 / P0-14: two gateway instances share one Redis circuit state.

    P0-convergence: each gateway gets its OWN Redis client/connection over a
    shared server — reusing one client object for both engines would not prove
    the state is distributed rather than process-local.
    """
    from fakeredis import FakeServer

    server = FakeServer()
    client_a = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    client_b = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    gateway_a = RouterEngine(
        _circuit_snapshot(), circuit_repository=RedisCircuitRepository(client_a)
    )
    gateway_b = RouterEngine(
        _circuit_snapshot(), circuit_repository=RedisCircuitRepository(client_b)
    )
    throttled = ResourceRef("conn", "cred-a", "model")

    before = await gateway_b.select_candidates_async("chat")
    assert [c.resource_ref.credential_scope for c in before] == ["cred-a", "cred-b"]

    await gateway_a.circuit_repository.trip_async(throttled, cooldown_seconds=60)

    after = await gateway_b.select_candidates_async("chat")
    creds = [c.resource_ref.credential_scope for c in after]
    assert creds == ["cred-b"], "instance B must hide only the throttled credential"


@pytest.mark.asyncio
async def test_e2e05_cooldown_expires_and_key_is_ttl_bound():
    """Cooldown is transient: TTL on the Redis key plus expiry on read."""
    authority = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisCircuitRepository(authority)
    ref = ResourceRef("conn", "cred-a", "model")

    await repo.trip_async(ref, cooldown_seconds=2)
    assert await repo.is_available_async(ref) is False
    ttl = await authority.ttl(f"circuit:{ref.key}")
    assert 0 < ttl <= 2, "cooldown key must carry a bounded Redis TTL"

    # Simulate expiry without sleeping on wall-clock TTL.
    await authority.delete(f"circuit:{ref.key}")
    assert await repo.is_available_async(ref) is True


@pytest.mark.asyncio
async def test_router_engine_awaits_async_circuit_repository_on_request_path():
    """select_candidates_async must not sync-block on an async Redis authority."""
    authority = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisCircuitRepository(authority)
    await repo.trip_async(ResourceRef("conn", "cred-a", "model"), cooldown_seconds=60)
    engine = RouterEngine(_circuit_snapshot(), circuit_repository=repo)

    selected = await engine.select_candidates_async("chat")

    assert [c.resource_ref.credential_scope for c in selected] == ["cred-b"]


def test_circuit_repositories_satisfy_protocol():
    assert isinstance(InMemoryCircuitRepository(), CircuitRepository)
    assert isinstance(
        RedisCircuitRepository(fakeredis.FakeRedis(decode_responses=True)),
        CircuitRepository,
    )


def _router_config() -> dict:
    return {
        "routes": {
            "main": {
                "candidates": [
                    {"upstream": "conn", "model": "model", "credential_id": "cred-a"},
                    {"upstream": "conn", "model": "model", "credential_id": "cred-b"},
                ]
            }
        },
        "upstreams": {
            "conn": {
                "base_url": "https://conn.test",
                "auth": {"mode": "bearer", "token_env": "CONN_TOKEN"},
            }
        },
        "logging": {"level": "CRITICAL"},
    }


@pytest.mark.asyncio
async def test_record_failure_trips_shared_authority_from_async_path(monkeypatch):
    """P0-14: a failure recorded through the request path reaches the Redis authority.

    The router must not leave the engine circuit trip as a thread-hopped sync
    call against an async client; the trip has to land in shared state so a
    second instance stops selecting the throttled credential.
    """
    from router import Candidate, SmartRouter

    authority = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisCircuitRepository(authority)
    monkeypatch.setattr(
        "router._build_circuit_repository", lambda: repo, raising=False
    )

    router = SmartRouter(_router_config())
    router.router_engine.circuit_repository = repo
    throttled = Candidate(
        upstream="conn", model="model", resource_key="conn:cred-a:model"
    )
    sibling = Candidate(upstream="conn", model="model", resource_key="conn:cred-b:model")

    await router._record_failure(
        throttled,
        429,
        "rate limited",
        {"kind": "RATE_LIMIT", "scope": "credential", "retry_after": "60",
         "reset_at": None, "consumption_uncertainty": "unknown"},
    )

    assert await repo.is_available_async(
        ResourceRef("conn", "cred-a", "model")
    ) is False
    assert await repo.is_available_async(
        ResourceRef("conn", "cred-b", "model")
    ) is True
    ttl = await authority.ttl("circuit:conn:cred-a:model")
    assert ttl > 0, "tripped credential must be stored as an expiring key"


def test_build_circuit_repository_uses_redis_authority(monkeypatch):
    """P0-14: with REDIS_URL configured, instances share a Redis circuit store."""
    import router as router_mod

    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    repo = router_mod._build_circuit_repository()
    assert isinstance(repo, RedisCircuitRepository)


def test_build_circuit_repository_falls_back_to_memory(monkeypatch):
    """Without REDIS_URL the engine keeps the process-local repository."""
    import router as router_mod

    monkeypatch.delenv("REDIS_URL", raising=False)
    repo = router_mod._build_circuit_repository()
    assert isinstance(repo, InMemoryCircuitRepository)
