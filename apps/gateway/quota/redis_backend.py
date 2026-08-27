"""Distributed quota reservations backed by redis.asyncio + Lua scripts.

Drop-in replacement for InMemoryQuotaReservations. All mutating operations
are atomic Lua scripts executed on a single Redis connection.

Data model
----------
quota:{resource_id}  HASH  – live counter + metadata
reservation:{id}     HASH  – active reservation (TTL 1 h)
group:{gpid}         ZSET  – peer lookup for shared-group sync

KEYS passed to Lua scripts are *bare* resource_ids; the Python layer
prepends ``quota:`` before calling Redis.  Reservation hashes store bare
IDs in JSON arrays to avoid double-prefix bugs.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from .reservations import (
    QuotaResource,
    QuotaObservation,
    QuotaReservationRequest,
    ReservationResult,
    ReservationBatchResult,
    ReconciliationResult,
    QuotaAdmissionResult,
)

logger = logging.getLogger(__name__)

# ─────────────────────────── Lua Scripts ─────────────────────────────

LUA_CHECK_MANY = r"""
-- KEYS[1..n] = bare resource_ids
-- ARGV[1..n] = required amount per resource
-- Returns cjson string: {accepted(bool), hard_failures{ressid:int},
--                        soft_pressure{ressid:int}, remaining{ressid:string}}

local hard_failures = {}
local soft_pressure = {}
local remaining_by_res = {}

for i = 1, #KEYS do
    local d = redis.call('HGETALL', 'quota:' .. KEYS[i])
    local info = {}
    for j = 1, #d, 2 do info[d[j]] = d[j + 1] end

    local limit   = tonumber(info['limit']) or 0
    local used_v  = tonumber(info['used']) or 0
    local safety  = tonumber(info['safety_buffer']) or 0
    local is_hard = (info['hard_limit'] == 'true')
    local gpid    = info['shared_group_id'] or ''

    -- Group-projected usage: max among all peers in the same group
    local proj_used = used_v
    if gpid ~= '' then
        local members = redis.call('ZRANGE', 'group:' .. gpid, 0, -1)
        local max_u = used_v
        for _, mkey in ipairs(members) do
            local md = redis.call('HGETALL', 'quota:' .. mkey)
            local mi = {}
            for k = 1, #md, 2 do mi[md[k]] = md[k + 1] end
            local mu = tonumber(mi['used']) or 0
            if mu > max_u then max_u = mu end
        end
        proj_used = max_u
    end

    local eff_remaining = math.max(0, limit - proj_used - safety)
    remaining_by_res[KEYS[i]] = tostring(eff_remaining)
    local required = tonumber(ARGV[i])

    if required > eff_remaining then
        if is_hard then
            hard_failures[KEYS[i]] = required - eff_remaining
        else
            soft_pressure[KEYS[i]] = required - eff_remaining
        end
    end
end

return cjson.encode({
    accepted        = (#hard_failures == 0),
    hard_failures   = hard_failures,
    soft_pressure   = soft_pressure,
    remaining       = remaining_by_res,
})
"""

LUA_RESERVE_MANY = r"""
-- KEYS[1..n] = bare resource_ids
-- ARGV[1]      = reservation_id
-- ARGV[2..n+1] = required amount per resource
-- ARGV[n+2]    = TTL seconds
-- Returns cjson: {accepted(bool), reason(string), remaining{ressid:string}}

local res_id  = ARGV[1]
local ttl     = tonumber(ARGV[#ARGV]) or 3600
local n_keys  = #KEYS

-- Idempotency: already-reserved key?
local existing_status = redis.call('HGET', 'reservation:' .. res_id, 'status')
if existing_status == 'ACCEPTED' then
    local rids = cjson.decode(redis.call('HGET', 'reservation:' .. res_id, 'rids'))
    local rems = {}
    for i = 1, #rids do
        local d = redis.call('HGETALL', 'quota:' .. rids[i])
        local info = {}
        for j = 1, #d, 2 do info[d[j]] = d[j + 1] end
        local lim   = tonumber(info['limit']) or 0
        local uv    = tonumber(info['used']) or 0
        local saf   = tonumber(info['safety_buffer']) or 0
        rems[rids[i]] = tostring(math.max(0, lim - uv - saf))
    end
    return cjson.encode({accepted = true, reason = '', remaining = rems})
end

-- Collect resource data for validation pass
local rids, limits, safeties, used_vals, g_pids = {}, {}, {}, {}, {}

for i = 1, n_keys do
    local d = redis.call('HGETALL', 'quota:' .. KEYS[i])
    local info = {}
    for j = 1, #d, 2 do info[d[j]] = d[j + 1] end
    rids[i]       = KEYS[i]
    limits[i]     = tonumber(info['limit']) or 0
    used_vals[i]  = tonumber(info['used']) or 0
    safeties[i]   = tonumber(info['safety_buffer']) or 0
    g_pids[i]     = info['shared_group_id'] or ''
end

-- Validation: check ALL resources first (all-or-nothing).
-- Include shared-group projection.
for i = 1, n_keys do
    local proj_used = used_vals[i]
    local gpid = g_pids[i]
    if gpid ~= '' then
        local members = redis.call('ZRANGE', 'group:' .. gpid, 0, -1)
        local max_u = used_vals[i]
        for _, mkey in ipairs(members) do
            local md = redis.call('HGETALL', 'quota:' .. mkey)
            local mi = {}
            for k = 1, #md, 2 do mi[md[k]] = md[k + 1] end
            local mu = tonumber(mi['used']) or 0
            if mu > max_u then max_u = mu end
        end
        proj_used = max_u
    end
    local eff = math.max(0, limits[i] - proj_used - safeties[i])
    if tonumber(ARGV[i + 1]) > eff then
        return cjson.encode({accepted = false, reason = 'quota_exceeded', remaining = {}})
    end
end

-- Mutation pass: increment used counters atomically
local new_remaining = {}

for i = 1, n_keys do
    local new_used = used_vals[i] + tonumber(ARGV[i + 1])
    redis.call('HSET', 'quota:' .. rids[i], 'used', tostring(new_used))
    new_remaining[rids[i]] = tostring(limits[i] - new_used - safeties[i])
    local gpid = g_pids[i]
    if gpid ~= '' then
        redis.call('ZADD', 'group:' .. gpid, new_used, rids[i])
    end
end

-- Record reservation with parallel dense arrays
redis.call('HMSET', 'reservation:' .. res_id,
    'status',     'ACCEPTED',
    'rids',       cjson.encode(rids),
    'amts',       cjson.encode(ARGV[2 .. n_keys + 1]),
    'created_at', tostring(math.floor(time.time())),
    'ttl',        tostring(ttl)
)
redis.call('EXPIRE', 'reservation:' .. res_id, ttl)

return cjson.encode({accepted = true, reason = '', remaining = new_remaining})
"""

LUA_RECONCILE = r"""
-- KEYS[1..n]   = bare resource_ids
-- ARGV[1]      = reservation_id
-- ARGV[2..n+1] = actual amount per resource (dense, keyed 1..n matching KEYS)
-- Returns cjson: {reserved{ressid:int}, released{ressid:string}, overshoot{ressid:string}, remaining{ressid:string}}

local res_id = ARGV[1]
local n_keys = #KEYS

local stored = redis.call('HGETALL', 'reservation:' .. res_id)
if #stored == 0 then
    return cjson.encode({error = 'unknown_reservation'})
end

local store_map = {}
for i = 1, #stored, 2 do store_map[stored[i]] = stored[i + 1] end

local rids_list = cjson.decode(store_map['rids'])
local amts_list = cjson.decode(store_map['amts'])

local reserved_amt: table<any,integer> = {}
local released: table<any,string> = {}
local overshoot: table<any,string> = {}
local new_rems: table<any,string> = {}

for i = 1, n_keys do
    local rid      = rids_list[i]
    local stored_amt = tonumber(amts_list[i]) or 0
    local act        = tonumber(ARGV[i + 1]) or 0
    local used       = tonumber(redis.call('HGET', 'quota:' .. rid, 'used')) or 0
    local new_used   = math.max(0, used - stored_amt + act)
    redis.call('HSET', 'quota:' .. rid, 'used', tostring(new_used))

    reserved_amt[rid] = stored_amt

    if act < stored_amt then
        released[rid] = tostring(stored_amt - act)
    end
    if act > stored_amt then
        overshoot[rid] = tostring(act - stored_amt)
    end

    local lim    = tonumber(redis.call('HGET', 'quota:' .. rid, 'limit')) or 0
    local saf    = tonumber(redis.call('HGET', 'quota:' .. rid, 'safety_buffer')) or 0
    new_rems[rid] = tostring(lim - new_used - saf)
end

redis.call('DEL', 'reservation:' .. res_id)
return cjson.encode({reserved = reserved_amt, released = released, overshoot = overshoot, remaining = new_rems})
"""

LUA_RELEASE = r"""
-- ARGV[1]    = reservation_id
-- KEYS unused — amounts stored in reservation hash
-- Returns cjson: {released(bool)}

local res_id = ARGV[1]

local stored = redis.call('HGETALL', 'reservation:' .. res_id)
if #stored == 0 then
    return cjson.encode({released = false})
end

local store_map = {}
for i = 1, #stored, 2 do store_map[stored[i]] = stored[i + 1] end

local rids_list = cjson.decode(store_map['rids'])
local amts_list = cjson.decode(store_map['amts'])
local count = 0

for i = 1, #rids_list do
    local rid   = rids_list[i]
    local amt   = tonumber(amts_list[i]) or 0
    local used  = tonumber(redis.call('HGET', 'quota:' .. rid, 'used')) or 0
    redis.call('HSET', 'quota:' .. rid, 'used', tostring(math.max(0, used - amt)))
    count = count + 1
end

redis.call('DEL', 'reservation:' .. res_id)
return cjson.encode({released = (count > 0)})
"""


# ─────────────────── Helpers ───────────────────


def _decode_json(raw: str | bytes) -> Any:
    """Decode response from Redis Lua cjson.encode()."""
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


# ─────────────────── Main Class ───────────────────


class RedisQuotaReservations:
    """Drop-in async replacement for InMemoryQuotaReservations using Redis.

    All mutating operations use single EVAL calls (Lua scripts) so they are
    truly atomic across processes.  Reads (snapshot) use HGETALL which is
    also atomic within a single command.

    Shared groups: resources sharing ``shared_group_id`` are linked via a
    Redis ZSET at ``group:{gpid}``.  Lua scripts aggregate projected usage
    across peers when checking capacity.

    Connection pool: ``max_connections=50``, ``socket_keepalive=True``.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0", *, ttl: int = 3600) -> None:
        import redis.asyncio as aioredis  # type: ignore[import-untyped]

        self._r = aioredis.from_url(
            redis_url,
            decode_responses=True,
            max_connections=50,
            socket_keepalive=True,
        )
        self._ttl = ttl

        # Register Lua scripts once (EVALSHA). Fallback to EVAL on cache miss.
        self._sha_map: dict[str, str | None] = {}
        self._script_bodies: dict[str, str] = {
            "check_many": LUA_CHECK_MANY,
            "reserve_many": LUA_RESERVE_MANY,
            "reconcile": LUA_RECONCILE,
            "release": LUA_RELEASE,
        }

    # ── Helpers ──────────────────────────────────────────────

    async def _ensure_sha(self, name: str) -> str | None:
        """Return SHA for a registered script (or None → will EVAL body)."""
        sha = self._sha_map.get(name)
        if sha is not None:
            return sha
        try:
            sha = await self._r.script_load(self._script_bodies[name])
            self._sha_map[name] = sha
            return sha
        except Exception as exc:
            logger.warning("Failed to load Lua script '%s': %s – will use EVAL", name, exc)
            self._sha_map[name] = None
            return None

    async def _eval_safe(self, script_name: str, num_keys: int, *keys_and_args: str) -> Any:
        """Evaluate a Lua script, falling back to EVAL on NoScriptError."""
        import redis.exceptions  # type: ignore[import-untyped]

        sha = await self._ensure_sha(script_name)
        if sha is not None:
            try:
                return await self._r.evalsha(sha, num_keys, *keys_and_args)
            except redis.exceptions.NoScriptError:
                pass  # Fall through to EVAL below

        # Fallback: EVAL with full script body
        return await self._r.eval(self._script_bodies[script_name], num_keys, *keys_and_args)

    @staticmethod
    def _prefix(resource_id: str) -> str:
        return f"quota:{resource_id}"

    @staticmethod
    def _parse_quota(raw: dict[str, str]) -> QuotaResource:
        shared_gid: str | None = raw.get("shared_group_id") or None
        return QuotaResource(
            resource_id=raw["resource_id"],
            scope=raw["scope"],
            metric=raw["metric"],
            limit=int(raw["limit"]),
            window_seconds=int(raw["window_seconds"]),
            used=int(raw.get("used", 0)),
            safety_buffer=int(raw.get("safety_buffer", 0)),
            hard_limit=(raw.get("hard_limit") == "true"),
            source=raw.get("source", "configured"),
            confidence=raw.get("confidence", "high"),
            shared_group_id=shared_gid,
        )

    # ── Public API (mirrors InMemoryQuotaReservations) ──────

    async def add_resource(self, resource: QuotaResource) -> QuotaResource:
        """Register / update a quota resource."""
        pipe = self._r.pipeline()
        pipe.hset(
            self._prefix(resource.resource_id),
            mapping={
                "scope": resource.scope,
                "metric": resource.metric,
                "limit": str(resource.limit),
                "used": str(resource.used),
                "safety_buffer": str(resource.safety_buffer),
                "hard_limit": "true" if resource.hard_limit else "false",
                "shared_group_id": resource.shared_group_id or "",
                "source": resource.source,
                "confidence": resource.confidence,
                "window_seconds": str(resource.window_seconds),
            },
        )
        if resource.shared_group_id:
            pipe.zadd(f"group:{resource.shared_group_id}", {resource.resource_id: resource.used})
        await pipe.execute()

        # Read back to confirm
        raw = await self._r.hgetall(self._prefix(resource.resource_id))
        raw["resource_id"] = resource.resource_id
        return self._parse_quota(raw)

    async def snapshot(self, resource_id: str) -> QuotaResource:
        raw = await self._r.hgetall(self._prefix(resource_id))
        if not raw:
            raise KeyError(f"unknown quota resource_id: {resource_id}")
        raw["resource_id"] = resource_id
        return self._parse_quota(raw)

    async def apply_observation(self, observation: QuotaObservation) -> QuotaResource:
        """Push live metrics into the store; propagate to shared-group peers."""
        key = self._prefix(observation.resource_id)
        updates: dict[str, str] = {
            "limit": str(observation.limit),
            "used": str(observation.used),
            "source": observation.source,
            "confidence": observation.confidence,
        }
        if observation.safety_buffer is not None:
            updates["safety_buffer"] = str(observation.safety_buffer)
        if observation.hard_limit is not None:
            updates["hard_limit"] = "true" if observation.hard_limit else "false"
        await self._r.hset(key, mapping=updates)

        # Propagate to shared-group peers: read current group, sync fields
        gpid = (await self._r.hget(key, "shared_group_id")) or ""
        if gpid and gpid != "":
            peers = await self._r.zrange(f"group:{gpid}", 0, -1)
            pipe = self._r.pipeline()
            for peer_key in peers:
                peer_full = self._prefix(peer_key) if ":" not in peer_key else peer_key
                pipe.hset(peer_full, mapping={
                    "limit": str(observation.limit),
                    "used": str(observation.used),
                    "safety_buffer": updates.get("safety_buffer", str(observation.safety_buffer or 0)),
                    "hard_limit": updates.get("hard_limit", "true"),
                })
            await pipe.execute()

        raw = await self._r.hgetall(key)
        raw["resource_id"] = observation.resource_id
        return self._parse_quota(raw)

    async def check_many(self, requests: list[QuotaReservationRequest]) -> QuotaAdmissionResult:
        """Non-mutating admission check across multiple resources/groups."""
        if not requests:
            raise ValueError("requests must not be empty")

        bare_keys = [r.resource_id for r in requests]
        args = [str(r.required) for r in requests]
        prefixed_keys = [self._prefix(rid) for rid in bare_keys]

        result_raw = await self._eval_safe("check_many", len(bare_keys), *prefixed_keys, *args)
        data = _decode_json(result_raw)

        return QuotaAdmissionResult(
            accepted=bool(data["accepted"]),
            hard_failures={str(k): int(v) for k, v in data["hard_failures"].items()},
            soft_pressure_by_resource={str(k): int(v) for k, v in data["soft_pressure"].items()},
            remaining_by_resource={str(k): int(v) for k, v in data["remaining"].items()},
        )

    async def reserve(
        self, *, resource_id: str, amount: int, reservation_id: str, risk_buffer: int = 0
    ) -> ReservationResult:
        """Atomic single-resource reservation."""
        req = QuotaReservationRequest(resource_id, amount, risk_buffer)
        batch = await self.reserve_many(reservation_id=reservation_id, requests=[req])
        if not batch.requests:
            return ReservationResult(
                reservation_id=reservation_id,
                resource_id=resource_id,
                amount=amount,
                accepted=batch.accepted,
                remaining=batch.remaining_by_resource.get(resource_id, 0),
            )
        return ReservationResult(
            reservation_id=reservation_id,
            resource_id=resource_id,
            amount=batch.requests[0].required,
            accepted=batch.accepted,
            remaining=batch.remaining_by_resource.get(resource_id, 0),
        )

    async def reserve_many(
        self, *, reservation_id: str, requests: list[QuotaReservationRequest]
    ) -> ReservationBatchResult:
        """Atomic all-or-nothing multi-resource reservation."""
        if not requests:
            raise ValueError("requests must not be empty")

        bare_keys = [r.resource_id for r in requests]
        amounts = [str(r.required) for r in requests]
        prefixed_keys = [self._prefix(rid) for rid in bare_keys]
        eval_args = [reservation_id, str(self._ttl)] + amounts

        result_raw = await self._eval_safe(
            "reserve_many", len(bare_keys), *prefixed_keys, *eval_args
        )
        data = _decode_json(result_raw)

        if data["accepted"]:
            return ReservationBatchResult(
                reservation_id=reservation_id,
                requests=tuple(requests),
                accepted=True,
                remaining_by_resource={
                    str(k): int(v) for k, v in data["remaining"].items()
                },
            )

        return ReservationBatchResult(
            reservation_id=reservation_id,
            requests=tuple(requests),
            accepted=False,
            remaining_by_resource={},
            reason=data.get("reason", "quota_exceeded"),
        )

    async def reconcile(
        self,
        reservation_id: str,
        actual_by_resource: dict[str, int],
    ) -> ReconciliationResult:
        """Post-completion reconciliation: replace reserved with actual usage."""
        bare_keys = sorted(actual_by_resource.keys())
        actuals = [str(actual_by_resource[rid]) for rid in bare_keys]
        prefixed_keys = [self._prefix(rid) for rid in bare_keys]
        eval_args = [reservation_id] + actuals

        result_raw = await self._eval_safe(
            "reconcile", len(bare_keys), *prefixed_keys, *eval_args
        )
        data = _decode_json(result_raw)

        if "error" in data:
            raise KeyError(f"unknown reservation_id: {reservation_id}")

        reserved_data = data.get("reserved", {})
        reserved_by_resource = {str(k): int(v) for k, v in reserved_data.items()} or {
            rid: 0 for rid in bare_keys
        }

        return ReconciliationResult(
            reservation_id=reservation_id,
            reserved_by_resource=reserved_by_resource,
            actual_by_resource=dict(actual_by_resource),
            released_by_resource={str(k): int(v) for k, v in data.get("released", {}).items()},
            overshoot_by_resource={str(k): int(v) for k, v in data.get("overshoot", {}).items()},
            remaining_by_resource={str(k): int(v) for k, v in data.get("remaining", {}).items()},
        )

    async def release(self, reservation_id: str) -> bool:
        """Release reserved capacity without adjusting used-to-actual."""
        # Fetch stored resource IDs from reservation hash
        res_data = await self._r.hgetall(f"reservation:{reservation_id}")
        if not res_data:
            return False

        rids_list = _decode_json(res_data.get("rids", "[]"))
        if not rids_list:
            return False

        # Release script uses reservation hash directly, keys arg is ignored
        result_raw = await self._eval_safe("release", 0, reservation_id)
        data = _decode_json(result_raw)
        return bool(data.get("released", False))

    @classmethod
    async def from_repository(cls, repository: Any) -> "RedisQuotaReservations":
        """Hydrate a fresh instance from DB state using pipeline batching."""
        instance = cls.__new__(cls)
        import redis.asyncio as aioredis  # type: ignore[import-untyped]

        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        instance._r = aioredis.from_url(
            redis_url,
            decode_responses=True,
            max_connections=50,
            socket_keepalive=True,
        )
        instance._ttl = 3600
        instance._sha_map = {}
        instance._script_bodies = {
            "check_many": LUA_CHECK_MANY,
            "reserve_many": LUA_RESERVE_MANY,
            "reconcile": LUA_RECONCILE,
            "release": LUA_RELEASE,
        }

        resources = await repository.list_resources()
        pipe = instance._r.pipeline()
        for res in resources:
            prefix = f"quota:{res.resource_id}"
            pipe.hset(prefix, mapping={
                "scope": res.scope,
                "metric": res.metric,
                "limit": str(res.limit),
                "used": str(res.used),
                "safety_buffer": str(res.safety_buffer),
                "hard_limit": "true" if res.hard_limit else "false",
                "shared_group_id": res.shared_group_id or "",
                "source": res.source,
                "confidence": res.confidence,
                "window_seconds": str(res.window_seconds),
            })
            if res.shared_group_id:
                pipe.zadd(f"group:{res.shared_group_id}", {res.resource_id: res.used})
        await pipe.execute()
        return instance
