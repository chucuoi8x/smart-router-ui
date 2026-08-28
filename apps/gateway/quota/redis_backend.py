"""Redis-backed quota store with atomic Lua reservation scripts."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .reservations import (
    QuotaAdmissionResult,
    QuotaObservation,
    QuotaReservationRequest,
    QuotaResource,
    ReconciliationResult,
    ReservationBatchResult,
    ReservationResult,
)
from .store import QuotaStore

logger = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_MAX_PARENT_DEPTH = 10


LUA_CHECK_MANY = r"""
local MAX_PARENT_DEPTH = tonumber(ARGV[1]) or 10
local rids = cjson.decode(ARGV[2])
local amounts = cjson.decode(ARGV[3])

local function quota_key(rid)
    return 'quota:' .. rid
end

local function hmap(rid)
    local raw = redis.call('HGETALL', quota_key(rid))
    if #raw == 0 then
        return nil
    end
    local info = {}
    for i = 1, #raw, 2 do
        info[raw[i]] = raw[i + 1]
    end
    return info
end

local function remaining(info)
    local limit = tonumber(info['limit']) or 0
    local used = tonumber(info['used']) or 0
    local safety = tonumber(info['safety_buffer']) or 0
    return math.max(0, limit - used - safety)
end

local function group_remaining(info)
    local gpid = info['shared_group_id'] or ''
    if gpid == '' then
        return remaining(info)
    end
    local peers = redis.call('ZRANGE', 'group:' .. gpid, 0, -1)
    local rem = nil
    for _, peer_rid in ipairs(peers) do
        local peer = hmap(peer_rid)
        if peer ~= nil then
            local peer_rem = remaining(peer)
            if rem == nil or peer_rem < rem then
                rem = peer_rem
            end
        end
    end
    if rem == nil then
        return remaining(info)
    end
    return rem
end

local function parent_remaining(rid)
    local current = rid
    local visited = {}
    local rem = nil
    for _ = 1, MAX_PARENT_DEPTH do
        local info = hmap(current)
        if info == nil then
            return nil, current
        end
        local parent_id = info['parent_id'] or ''
        if parent_id == '' then
            break
        end
        if visited[parent_id] then
            return nil, parent_id
        end
        visited[current] = true
        local parent = hmap(parent_id)
        if parent == nil then
            return nil, parent_id
        end
        local parent_rem = remaining(parent)
        if rem == nil or parent_rem < rem then
            rem = parent_rem
        end
        current = parent_id
    end
    return rem
end

local function effective_remaining(rid)
    local info = hmap(rid)
    if info == nil then
        return nil, 'unknown_resource', rid
    end
    local resource_rem = remaining(info)
    local group_rem = group_remaining(info)
    local parent_rem, parent_err = parent_remaining(rid)
    if parent_err ~= nil then
        return nil, 'unknown_parent', parent_err
    end
    local eff = resource_rem
    if group_rem < eff then eff = group_rem end
    if parent_rem ~= nil and parent_rem < eff then eff = parent_rem end
    return eff, nil, nil, info
end

local hard_failures = {}
local soft_pressure = {}
local remaining_by_resource = {}
local hard_count = 0

local projected_by_rid = {}
local projected_by_group = {}
local projected_by_parent = {}

for i = 1, #rids do
    local rid = rids[i]
    local required = tonumber(amounts[i]) or 0
    local eff, err, detail, info = effective_remaining(rid)
    if err ~= nil then
        return cjson.encode({error = err, resource_id = detail})
    end

    local projected = (projected_by_rid[rid] or 0) + required
    local gpid = info['shared_group_id'] or ''
    if gpid ~= '' then
        projected_by_group[gpid] = (projected_by_group[gpid] or 0) + required
        projected = math.max(projected, projected_by_group[gpid])
    end

    local current = rid
    for _ = 1, MAX_PARENT_DEPTH do
        local cur_info = hmap(current)
        if cur_info == nil then break end
        local parent_id = cur_info['parent_id'] or ''
        if parent_id == '' then break end
        projected_by_parent[parent_id] = (projected_by_parent[parent_id] or 0) + required
        projected = math.max(projected, projected_by_parent[parent_id])
        current = parent_id
    end

    remaining_by_resource[rid] = tostring(eff)
    local shortfall = projected - eff
    if shortfall > 0 then
        if info['hard_limit'] == 'true' then
            hard_failures[rid] = shortfall
            hard_count = hard_count + 1
        else
            soft_pressure[rid] = shortfall
        end
    end
    projected_by_rid[rid] = (projected_by_rid[rid] or 0) + required
end

return cjson.encode({
    accepted = (hard_count == 0),
    hard_failures = hard_failures,
    soft_pressure = soft_pressure,
    remaining = remaining_by_resource
})
"""


LUA_RESERVE_MANY = r"""
local MAX_PARENT_DEPTH = tonumber(ARGV[1]) or 10
local reservation_id = ARGV[2]
local rids_json = ARGV[3]
local amounts_json = ARGV[4]
local ttl = tonumber(ARGV[5]) or 3600
local rids = cjson.decode(rids_json)
local amounts = cjson.decode(amounts_json)

local function quota_key(rid)
    return 'quota:' .. rid
end

local function hmap(rid)
    local raw = redis.call('HGETALL', quota_key(rid))
    if #raw == 0 then
        return nil
    end
    local info = {}
    for i = 1, #raw, 2 do
        info[raw[i]] = raw[i + 1]
    end
    return info
end

local function remaining(info)
    local limit = tonumber(info['limit']) or 0
    local used = tonumber(info['used']) or 0
    local safety = tonumber(info['safety_buffer']) or 0
    return math.max(0, limit - used - safety)
end

local function group_remaining(info)
    local gpid = info['shared_group_id'] or ''
    if gpid == '' then
        return remaining(info)
    end
    local peers = redis.call('ZRANGE', 'group:' .. gpid, 0, -1)
    local rem = nil
    for _, peer_rid in ipairs(peers) do
        local peer = hmap(peer_rid)
        if peer ~= nil then
            local peer_rem = remaining(peer)
            if rem == nil or peer_rem < rem then
                rem = peer_rem
            end
        end
    end
    if rem == nil then
        return remaining(info)
    end
    return rem
end

local function parent_remaining(rid)
    local current = rid
    local visited = {}
    local rem = nil
    for _ = 1, MAX_PARENT_DEPTH do
        local info = hmap(current)
        if info == nil then
            return nil, current
        end
        local parent_id = info['parent_id'] or ''
        if parent_id == '' then
            break
        end
        if visited[parent_id] then
            return nil, parent_id
        end
        visited[current] = true
        local parent = hmap(parent_id)
        if parent == nil then
            return nil, parent_id
        end
        local parent_rem = remaining(parent)
        if rem == nil or parent_rem < rem then
            rem = parent_rem
        end
        current = parent_id
    end
    return rem
end

local function effective_remaining(rid)
    local info = hmap(rid)
    if info == nil then
        return nil, 'unknown_resource', rid
    end
    local resource_rem = remaining(info)
    local group_rem = group_remaining(info)
    local parent_rem, parent_err = parent_remaining(rid)
    if parent_err ~= nil then
        return nil, 'unknown_parent', parent_err
    end
    local eff = resource_rem
    if group_rem < eff then eff = group_rem end
    if parent_rem ~= nil and parent_rem < eff then eff = parent_rem end
    return eff, nil, nil, info
end

local res_key = 'reservation:' .. reservation_id
local existing_status = redis.call('HGET', res_key, 'status')
if existing_status ~= false then
    local stored_rids = redis.call('HGET', res_key, 'rids') or ''
    local stored_amounts = redis.call('HGET', res_key, 'amounts') or ''
    if stored_rids ~= rids_json or stored_amounts ~= amounts_json then
        return cjson.encode({error = 'reservation_id_conflict'})
    end
    local rems = {}
    for i = 1, #rids do
        local eff = effective_remaining(rids[i])
        rems[rids[i]] = tostring(eff or 0)
    end
    return cjson.encode({
        accepted = (existing_status == 'ACCEPTED'),
        reason = redis.call('HGET', res_key, 'reason') or '',
        rejected_resource_id = redis.call('HGET', res_key, 'rejected_resource_id') or '',
        remaining = rems,
        effective_remaining = rems
    })
end

local hard = {}
local req_by_rid = {}
local req_by_group = {}
local group_first_rid = {}
local req_by_parent = {}
local unique_rids = {}

for i = 1, #rids do
    local rid = rids[i]
    local required = tonumber(amounts[i]) or 0
    local eff, err, detail, info = effective_remaining(rid)
    if err ~= nil then
        return cjson.encode({error = err, resource_id = detail})
    end
    if info['hard_limit'] ~= 'true' then
        return cjson.encode({error = 'cannot_reserve_soft_quota', resource_id = rid})
    end
    if req_by_rid[rid] == nil then
        table.insert(unique_rids, rid)
        req_by_rid[rid] = 0
    end
    req_by_rid[rid] = req_by_rid[rid] + required

    local gpid = info['shared_group_id'] or ''
    if gpid ~= '' then
        req_by_group[gpid] = (req_by_group[gpid] or 0) + required
        if group_first_rid[gpid] == nil then group_first_rid[gpid] = rid end
    end

    local current = rid
    for _ = 1, MAX_PARENT_DEPTH do
        local cur_info = hmap(current)
        if cur_info == nil then break end
        local parent_id = cur_info['parent_id'] or ''
        if parent_id == '' then break end
        req_by_parent[parent_id] = (req_by_parent[parent_id] or 0) + required
        current = parent_id
    end
end

for _, rid in ipairs(unique_rids) do
    local eff = effective_remaining(rid)
    if req_by_rid[rid] > eff then
        hard[rid] = req_by_rid[rid] - eff
    end
end

for gpid, required in pairs(req_by_group) do
    local peers = redis.call('ZRANGE', 'group:' .. gpid, 0, -1)
    local group_rem = nil
    for _, peer_rid in ipairs(peers) do
        local peer = hmap(peer_rid)
        if peer ~= nil then
            local peer_rem = remaining(peer)
            if group_rem == nil or peer_rem < group_rem then
                group_rem = peer_rem
            end
        end
    end
    if group_rem ~= nil and required > group_rem then
        local rid = group_first_rid[gpid]
        hard[rid] = required - group_rem
    end
end

for parent_id, required in pairs(req_by_parent) do
    local parent = hmap(parent_id)
    if parent == nil then
        return cjson.encode({error = 'unknown_parent', resource_id = parent_id})
    end
    local parent_rem = remaining(parent)
    if required > parent_rem then
        hard[parent_id] = required - parent_rem
    end
end

local rejected_rid = nil
for i = 1, #rids do
    if hard[rids[i]] ~= nil then
        rejected_rid = rids[i]
        break
    end
end
if rejected_rid == nil then
    for parent_id, _ in pairs(req_by_parent) do
        if hard[parent_id] ~= nil then
            rejected_rid = parent_id
            break
        end
    end
end

if rejected_rid ~= nil then
    local rems = {}
    local effs = {}
    for i = 1, #rids do
        local info = hmap(rids[i])
        rems[rids[i]] = tostring(remaining(info))
        effs[rids[i]] = tostring(effective_remaining(rids[i]) or 0)
    end
    redis.call('HMSET', res_key,
        'status', 'REJECTED',
        'reason', 'quota_exceeded',
        'rejected_resource_id', rejected_rid,
        'rids', rids_json,
        'amounts', amounts_json
    )
    redis.call('EXPIRE', res_key, ttl)
    return cjson.encode({accepted = false, reason = 'quota_exceeded', rejected_resource_id = rejected_rid, remaining = rems, effective_remaining = effs})
end

local mutated_groups = {}
for _, rid in ipairs(unique_rids) do
    local info = hmap(rid)
    local gpid = info['shared_group_id'] or ''
    if gpid ~= '' then
        if mutated_groups[gpid] == nil then
            mutated_groups[gpid] = true
            local peers = redis.call('ZRANGE', 'group:' .. gpid, 0, -1)
            local max_used = tonumber(info['used']) or 0
            for _, peer_rid in ipairs(peers) do
                local peer = hmap(peer_rid)
                if peer ~= nil then
                    local peer_used = tonumber(peer['used']) or 0
                    if peer_used > max_used then max_used = peer_used end
                end
            end
            local new_used = max_used + req_by_group[gpid]
            for _, peer_rid in ipairs(peers) do
                redis.call('HSET', quota_key(peer_rid), 'used', tostring(new_used))
                redis.call('ZADD', 'group:' .. gpid, new_used, peer_rid)
            end
        end
    else
        local used = tonumber(info['used']) or 0
        redis.call('HSET', quota_key(rid), 'used', tostring(used + req_by_rid[rid]))
    end
end

for parent_id, required in pairs(req_by_parent) do
    redis.call('HINCRBY', quota_key(parent_id), 'used', required)
end

local rems = {}
local effs = {}
for i = 1, #rids do
    local info = hmap(rids[i])
    rems[rids[i]] = tostring(remaining(info))
    effs[rids[i]] = tostring(effective_remaining(rids[i]) or 0)
end

redis.call('HMSET', res_key,
    'status', 'ACCEPTED',
    'reason', '',
    'rejected_resource_id', '',
    'rids', rids_json,
    'amounts', amounts_json
)
redis.call('EXPIRE', res_key, ttl)
return cjson.encode({accepted = true, reason = '', rejected_resource_id = '', remaining = rems, effective_remaining = effs})
"""


LUA_RECONCILE = r"""
local MAX_PARENT_DEPTH = 10
local reservation_id = ARGV[1]
local actual_json = ARGV[2]
local actual = cjson.decode(actual_json)
local res_key = 'reservation:' .. reservation_id

local function quota_key(rid)
    return 'quota:' .. rid
end

local function hmap(rid)
    local raw = redis.call('HGETALL', quota_key(rid))
    if #raw == 0 then return nil end
    local info = {}
    for i = 1, #raw, 2 do info[raw[i]] = raw[i + 1] end
    return info
end

local function remaining(info)
    local limit = tonumber(info['limit']) or 0
    local used = tonumber(info['used']) or 0
    local safety = tonumber(info['safety_buffer']) or 0
    return math.max(0, limit - used - safety)
end

local stored = redis.call('HGETALL', res_key)
if #stored == 0 then
    return cjson.encode({error = 'unknown_reservation'})
end
local store = {}
for i = 1, #stored, 2 do store[stored[i]] = stored[i + 1] end
if store['status'] ~= 'ACCEPTED' then
    return cjson.encode({error = 'cannot_reconcile_rejected'})
end

local rids = cjson.decode(store['rids'])
local amounts = cjson.decode(store['amounts'])
local reserved_map = {}
local reserved = {}
local released = {}
local overshoot = {}
local remaining_by_resource = {}
local group_reserved = {}
local group_actual = {}
local group_seen = {}
local group_members = {}
local own_delta = {}
local parent_delta = {}

for i = 1, #rids do
    local rid = rids[i]
    local reserved_amount = tonumber(amounts[i]) or 0
    reserved_map[rid] = true
    reserved[rid] = (reserved[rid] or 0) + reserved_amount
end

for rid, _ in pairs(actual) do
    if reserved_map[rid] == nil then
        return cjson.encode({error = 'unexpected_actual', resource_id = rid})
    end
end

for rid, reserved_amount in pairs(reserved) do
    local actual_amount = tonumber(actual[rid])
    if actual_amount == nil then
        return cjson.encode({error = 'missing_actual', resource_id = rid})
    end
    local info = hmap(rid)
    if info == nil then
        return cjson.encode({error = 'unknown_resource', resource_id = rid})
    end
    if actual_amount < reserved_amount then released[rid] = reserved_amount - actual_amount end
    if actual_amount > reserved_amount then overshoot[rid] = actual_amount - reserved_amount end

    local gpid = info['shared_group_id'] or ''
    if gpid ~= '' then
        group_reserved[gpid] = (group_reserved[gpid] or 0) + reserved_amount
        group_actual[gpid] = (group_actual[gpid] or 0) + actual_amount
        if group_seen[gpid] == nil then
            group_seen[gpid] = true
            group_members[gpid] = redis.call('ZRANGE', 'group:' .. gpid, 0, -1)
        end
    else
        own_delta[rid] = actual_amount - reserved_amount
    end

    local current = rid
    for _ = 1, MAX_PARENT_DEPTH do
        local cur_info = hmap(current)
        if cur_info == nil then break end
        local parent_id = cur_info['parent_id'] or ''
        if parent_id == '' then break end
        parent_delta[parent_id] = (parent_delta[parent_id] or 0) + (actual_amount - reserved_amount)
        current = parent_id
    end
end

for rid, delta in pairs(own_delta) do
    local info = hmap(rid)
    local used = tonumber(info['used']) or 0
    redis.call('HSET', quota_key(rid), 'used', tostring(math.max(0, used + delta)))
end

for gpid, reserved_amount in pairs(group_reserved) do
    local actual_amount = group_actual[gpid] or 0
    local delta = actual_amount - reserved_amount
    local peers = group_members[gpid]
    local max_used = 0
    for _, peer_rid in ipairs(peers) do
        local peer = hmap(peer_rid)
        if peer ~= nil then
            local used = tonumber(peer['used']) or 0
            if used > max_used then max_used = used end
        end
    end
    local new_used = math.max(0, max_used + delta)
    for _, peer_rid in ipairs(peers) do
        redis.call('HSET', quota_key(peer_rid), 'used', tostring(new_used))
        redis.call('ZADD', 'group:' .. gpid, new_used, peer_rid)
    end
end

for parent_id, delta in pairs(parent_delta) do
    local parent = hmap(parent_id)
    if parent ~= nil then
        local used = tonumber(parent['used']) or 0
        redis.call('HSET', quota_key(parent_id), 'used', tostring(math.max(0, used + delta)))
    end
end

for rid, _ in pairs(reserved) do
    local info = hmap(rid)
    remaining_by_resource[rid] = tostring(remaining(info))
end

redis.call('DEL', res_key)
return cjson.encode({reserved = reserved, actual = actual, released = released, overshoot = overshoot, remaining = remaining_by_resource})
"""


LUA_RELEASE = r"""
local MAX_PARENT_DEPTH = 10
local reservation_id = ARGV[1]
local res_key = 'reservation:' .. reservation_id

local function quota_key(rid)
    return 'quota:' .. rid
end

local function hmap(rid)
    local raw = redis.call('HGETALL', quota_key(rid))
    if #raw == 0 then return nil end
    local info = {}
    for i = 1, #raw, 2 do info[raw[i]] = raw[i + 1] end
    return info
end

local stored = redis.call('HGETALL', res_key)
if #stored == 0 then
    return cjson.encode({released = false})
end
local store = {}
for i = 1, #stored, 2 do store[stored[i]] = stored[i + 1] end
if store['status'] ~= 'ACCEPTED' then
    return cjson.encode({released = false})
end

local rids = cjson.decode(store['rids'])
local amounts = cjson.decode(store['amounts'])
local reserved = {}
local group_reserved = {}
local group_seen = {}
local group_members = {}
local parent_reserved = {}

for i = 1, #rids do
    local rid = rids[i]
    local amount = tonumber(amounts[i]) or 0
    reserved[rid] = (reserved[rid] or 0) + amount
end

for rid, amount in pairs(reserved) do
    local info = hmap(rid)
    if info ~= nil then
        local gpid = info['shared_group_id'] or ''
        if gpid ~= '' then
            group_reserved[gpid] = (group_reserved[gpid] or 0) + amount
            if group_seen[gpid] == nil then
                group_seen[gpid] = true
                group_members[gpid] = redis.call('ZRANGE', 'group:' .. gpid, 0, -1)
            end
        else
            local used = tonumber(info['used']) or 0
            redis.call('HSET', quota_key(rid), 'used', tostring(math.max(0, used - amount)))
        end

        local current = rid
        for _ = 1, MAX_PARENT_DEPTH do
            local cur_info = hmap(current)
            if cur_info == nil then break end
            local parent_id = cur_info['parent_id'] or ''
            if parent_id == '' then break end
            parent_reserved[parent_id] = (parent_reserved[parent_id] or 0) + amount
            current = parent_id
        end
    end
end

for gpid, amount in pairs(group_reserved) do
    local peers = group_members[gpid]
    local max_used = 0
    for _, peer_rid in ipairs(peers) do
        local peer = hmap(peer_rid)
        if peer ~= nil then
            local used = tonumber(peer['used']) or 0
            if used > max_used then max_used = used end
        end
    end
    local new_used = math.max(0, max_used - amount)
    for _, peer_rid in ipairs(peers) do
        redis.call('HSET', quota_key(peer_rid), 'used', tostring(new_used))
        redis.call('ZADD', 'group:' .. gpid, new_used, peer_rid)
    end
end

for parent_id, amount in pairs(parent_reserved) do
    local parent = hmap(parent_id)
    if parent ~= nil then
        local used = tonumber(parent['used']) or 0
        redis.call('HSET', quota_key(parent_id), 'used', tostring(math.max(0, used - amount)))
    end
end

redis.call('DEL', res_key)
return cjson.encode({released = true})
"""


def _decode_json(raw: str | bytes) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


def _as_int_map(data: Any) -> dict[str, int]:
    if not isinstance(data, dict):
        return {}
    return {str(key): int(value) for key, value in data.items()}


class RedisQuotaStore(QuotaStore):
    """Redis implementation of QuotaStore.

    Resource and reservation identifiers are validated before creating Redis
    keys. check_many/reserve_many/reconcile/release use Lua so their reads and
    mutations are atomic on a single Redis connection.
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
        self._max_parent_depth = _MAX_PARENT_DEPTH
        self._sha_map: dict[str, str | None] = {}
        self._script_bodies = {
            "check_many": LUA_CHECK_MANY,
            "reserve_many": LUA_RESERVE_MANY,
            "reconcile": LUA_RECONCILE,
            "release": LUA_RELEASE,
        }

    async def _ensure_sha(self, name: str) -> str | None:
        sha = self._sha_map.get(name)
        if sha is not None:
            return sha
        try:
            sha = await self._r.script_load(self._script_bodies[name])
            self._sha_map[name] = sha
            return sha
        except Exception as exc:
            logger.warning("Failed to load Lua script %s: %s; falling back to EVAL", name, exc)
            self._sha_map[name] = None
            return None

    async def _eval_safe(self, script_name: str, num_keys: int, *keys_and_args: str) -> Any:
        import redis.exceptions  # type: ignore[import-untyped]

        sha = await self._ensure_sha(script_name)
        if sha is not None:
            try:
                return await self._r.evalsha(sha, num_keys, *keys_and_args)
            except redis.exceptions.NoScriptError:
                pass
        return await self._r.eval(self._script_bodies[script_name], num_keys, *keys_and_args)

    @staticmethod
    def _validate_id(value: str, *, field: str) -> None:
        if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
            raise ValueError(f"invalid {field}: {value!r}")

    @classmethod
    def _validate_resource_id(cls, resource_id: str) -> None:
        cls._validate_id(resource_id, field="resource_id")

    @classmethod
    def _validate_reservation_id(cls, reservation_id: str) -> None:
        cls._validate_id(reservation_id, field="reservation_id")

    @staticmethod
    def _prefix(resource_id: str) -> str:
        return f"quota:{resource_id}"

    @staticmethod
    def _resource_id_from_key(key: str) -> str:
        return key.removeprefix("quota:")

    @staticmethod
    def _parse_quota(raw: dict[str, str], resource_id: str) -> QuotaResource:
        return QuotaResource(
            resource_id=resource_id,
            scope=raw["scope"],
            metric=raw["metric"],
            limit=int(raw["limit"]),
            window_seconds=int(raw["window_seconds"]),
            used=int(raw.get("used", 0)),
            safety_buffer=int(raw.get("safety_buffer", 0)),
            hard_limit=raw.get("hard_limit", "true") == "true",
            source=raw.get("source", "configured"),
            confidence=raw.get("confidence", "high"),
            shared_group_id=raw.get("shared_group_id") or None,
            parent_id=raw.get("parent_id") or None,
        )

    def _raise_script_error(self, data: dict[str, Any]) -> None:
        error = data.get("error")
        resource_id = data.get("resource_id")
        if error in {"unknown_resource", "unknown_parent"}:
            raise KeyError(f"unknown quota resource_id: {resource_id}")
        if error == "unknown_reservation":
            raise KeyError("unknown reservation_id")
        if error == "reservation_id_conflict":
            raise ValueError("reservation_id conflict")
        if error == "cannot_reserve_soft_quota":
            raise ValueError(f"cannot reserve soft quota resource: {resource_id}")
        if error == "cannot_reconcile_rejected":
            raise ValueError("cannot reconcile a rejected reservation")
        if error == "missing_actual":
            raise ValueError(f"actual usage missing resource_id: {resource_id}")
        if error == "unexpected_actual":
            raise ValueError(f"actual usage contains unknown resource_id: {resource_id}")
        if error:
            raise ValueError(str(error))

    def _request_payload(self, requests: list[QuotaReservationRequest]) -> tuple[list[str], str, str]:
        if not requests:
            raise ValueError("requests must not be empty")
        rids = [request.resource_id for request in requests]
        for resource_id in rids:
            self._validate_resource_id(resource_id)
        amounts = [request.required for request in requests]
        return rids, json.dumps(rids, separators=(",", ":")), json.dumps(amounts, separators=(",", ":"))

    async def add_resource(self, resource: QuotaResource) -> QuotaResource:
        self._validate_resource_id(resource.resource_id)
        if resource.shared_group_id is not None:
            self._validate_id(resource.shared_group_id, field="shared_group_id")
        if resource.parent_id is not None:
            self._validate_resource_id(resource.parent_id)

        key = self._prefix(resource.resource_id)
        previous_group = await self._r.hget(key, "shared_group_id")
        pipe = self._r.pipeline()
        if previous_group and previous_group != resource.shared_group_id:
            pipe.zrem(f"group:{previous_group}", resource.resource_id)
        pipe.hset(
            key,
            mapping={
                "scope": resource.scope,
                "metric": resource.metric,
                "limit": str(resource.limit),
                "used": str(resource.used),
                "safety_buffer": str(resource.safety_buffer),
                "hard_limit": "true" if resource.hard_limit else "false",
                "shared_group_id": resource.shared_group_id or "",
                "parent_id": resource.parent_id or "",
                "source": resource.source,
                "confidence": resource.confidence,
                "window_seconds": str(resource.window_seconds),
            },
        )
        if resource.shared_group_id:
            pipe.zadd(f"group:{resource.shared_group_id}", {resource.resource_id: resource.used})
        await pipe.execute()

        if resource.shared_group_id:
            await self._sync_shared_group(resource.shared_group_id)
        return await self.snapshot(resource.resource_id)

    async def _sync_shared_group(self, shared_group_id: str) -> None:
        peers = await self._r.zrange(f"group:{shared_group_id}", 0, -1)
        resources: list[QuotaResource] = []
        for peer_id in peers:
            raw = await self._r.hgetall(self._prefix(peer_id))
            if raw:
                resources.append(self._parse_quota(raw, peer_id))
        if not resources:
            return
        synced_used = max(resource.used for resource in resources)
        synced_limit = max(synced_used, min(resource.limit for resource in resources))
        synced_safety = min(synced_limit, max(resource.safety_buffer for resource in resources))
        synced_hard = any(resource.hard_limit for resource in resources)
        pipe = self._r.pipeline()
        for resource in resources:
            pipe.hset(
                self._prefix(resource.resource_id),
                mapping={
                    "limit": str(synced_limit),
                    "used": str(synced_used),
                    "safety_buffer": str(synced_safety),
                    "hard_limit": "true" if synced_hard else "false",
                },
            )
            pipe.zadd(f"group:{shared_group_id}", {resource.resource_id: synced_used})
        await pipe.execute()

    async def snapshot(self, resource_id: str) -> QuotaResource:
        self._validate_resource_id(resource_id)
        raw = await self._r.hgetall(self._prefix(resource_id))
        if not raw:
            raise KeyError(f"unknown quota resource_id: {resource_id}")
        return self._parse_quota(raw, resource_id)

    async def list_resources(self) -> list[QuotaResource]:
        resources: list[QuotaResource] = []
        async for key in self._r.scan_iter(match="quota:*"):
            resource_id = self._resource_id_from_key(str(key))
            raw = await self._r.hgetall(key)
            if raw:
                resources.append(self._parse_quota(raw, resource_id))
        return resources

    async def apply_observation(self, observation: QuotaObservation) -> QuotaResource:
        self._validate_resource_id(observation.resource_id)
        current = await self.snapshot(observation.resource_id)
        updates = {
            "limit": str(observation.limit),
            "used": str(observation.used),
            "source": observation.source,
            "confidence": observation.confidence,
        }
        if observation.safety_buffer is not None:
            updates["safety_buffer"] = str(observation.safety_buffer)
        if observation.hard_limit is not None:
            updates["hard_limit"] = "true" if observation.hard_limit else "false"
        await self._r.hset(self._prefix(observation.resource_id), mapping=updates)

        if current.shared_group_id:
            peer_updates = {
                "limit": str(observation.limit),
                "used": str(observation.used),
            }
            if observation.safety_buffer is not None:
                peer_updates["safety_buffer"] = str(observation.safety_buffer)
            if observation.hard_limit is not None:
                peer_updates["hard_limit"] = "true" if observation.hard_limit else "false"
            peers = await self._r.zrange(f"group:{current.shared_group_id}", 0, -1)
            pipe = self._r.pipeline()
            for peer_id in peers:
                pipe.hset(self._prefix(peer_id), mapping=peer_updates)
                pipe.zadd(f"group:{current.shared_group_id}", {peer_id: observation.used})
            await pipe.execute()

        return await self.snapshot(observation.resource_id)

    async def check_many(self, requests: list[QuotaReservationRequest]) -> QuotaAdmissionResult:
        rids, rids_json, amounts_json = self._request_payload(requests)
        result_raw = await self._eval_safe(
            "check_many",
            len(rids),
            *rids,
            str(self._max_parent_depth),
            rids_json,
            amounts_json,
        )
        data = _decode_json(result_raw)
        if "error" in data:
            self._raise_script_error(data)
        return QuotaAdmissionResult(
            accepted=bool(data["accepted"]),
            hard_failures=_as_int_map(data.get("hard_failures", {})),
            soft_pressure_by_resource=_as_int_map(data.get("soft_pressure", {})),
            remaining_by_resource=_as_int_map(data.get("remaining", {})),
        )

    async def reserve(
        self,
        *,
        resource_id: str,
        amount: int,
        reservation_id: str,
        risk_buffer: int = 0,
    ) -> ReservationResult:
        request = QuotaReservationRequest(resource_id, amount, risk_buffer)
        batch = await self.reserve_many(reservation_id=reservation_id, requests=[request])
        return ReservationResult(
            reservation_id=reservation_id,
            resource_id=resource_id,
            amount=request.required,
            accepted=batch.accepted,
            remaining=batch.remaining_by_resource.get(resource_id, 0),
            reason=batch.reason,
        )

    async def reserve_many(
        self,
        *,
        reservation_id: str,
        requests: list[QuotaReservationRequest],
    ) -> ReservationBatchResult:
        self._validate_reservation_id(reservation_id)
        rids, rids_json, amounts_json = self._request_payload(requests)
        result_raw = await self._eval_safe(
            "reserve_many",
            len(rids),
            *rids,
            str(self._max_parent_depth),
            reservation_id,
            rids_json,
            amounts_json,
            str(self._ttl),
        )
        data = _decode_json(result_raw)
        if "error" in data:
            self._raise_script_error(data)
        return ReservationBatchResult(
            reservation_id=reservation_id,
            requests=tuple(requests),
            accepted=bool(data["accepted"]),
            remaining_by_resource=_as_int_map(data.get("remaining", {})),
            reason=data.get("reason") or None,
            rejected_resource_id=data.get("rejected_resource_id") or None,
            effective_remaining_by_resource=_as_int_map(data.get("effective_remaining", {})),
        )

    async def reconcile(
        self,
        reservation_id: str,
        actual_by_resource: dict[str, int],
    ) -> ReconciliationResult:
        self._validate_reservation_id(reservation_id)
        for resource_id, amount in actual_by_resource.items():
            self._validate_resource_id(resource_id)
            if amount < 0:
                raise ValueError("actual amounts must be non-negative")
        result_raw = await self._eval_safe(
            "reconcile",
            0,
            reservation_id,
            json.dumps(actual_by_resource, separators=(",", ":")),
        )
        data = _decode_json(result_raw)
        if "error" in data:
            self._raise_script_error(data)
        return ReconciliationResult(
            reservation_id=reservation_id,
            reserved_by_resource=_as_int_map(data.get("reserved", {})),
            actual_by_resource=_as_int_map(data.get("actual", {})),
            released_by_resource=_as_int_map(data.get("released", {})),
            overshoot_by_resource=_as_int_map(data.get("overshoot", {})),
            remaining_by_resource=_as_int_map(data.get("remaining", {})),
        )

    async def release(self, reservation_id: str) -> bool:
        self._validate_reservation_id(reservation_id)
        result_raw = await self._eval_safe("release", 0, reservation_id)
        data = _decode_json(result_raw)
        return bool(data.get("released", False))

    @classmethod
    async def from_repository(cls, repository: Any) -> "RedisQuotaStore":
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        instance = cls(redis_url=redis_url)
        resources = await repository.list_resources()
        for resource in resources:
            await instance.add_resource(resource)
        return instance


class RedisQuotaReservations(RedisQuotaStore):
    """Backward-compatible name for the Redis quota store."""
