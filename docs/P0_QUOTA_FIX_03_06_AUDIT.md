# FIX-03 through FIX-06 quota audit

## Delivered vertical slice (FIX-05)

RedisQuotaStore now serializes and restores `reset_at` (ISO-8601) and
`window_metadata` (JSON). Legacy hashes without either field still hydrate.
Existing parent/shared identity, limit, used, safety, source, confidence, scope,
metric and window_seconds remain intact. RuntimeQuotaIndex retains metadata and
isolates nested metadata from readers.

Evidence: `tests/unit/test_quota_reset_metadata.py` writes through
QuotaResourceRepository, stores through one RedisQuotaStore/FakeAsyncRedis,
reads through a second RedisQuotaStore/FakeAsyncRedis sharing only FakeServer,
and rebuilds RuntimeQuotaIndex twice. SQLite exercises repository mapping; this
is not a PostgreSQL integration proof or actual process restart.

## Open acceptance gaps

- FIX-03: RouterEngine `_known_quota_resource_ids` still excludes metrics other
  than requests. Admission uses amount=1; no generic request demand propagated
  from live payload. Tokens, credits, USD, concurrency, compute and native units
  need shared estimation and atomic-reservation parity. Reservation remains
  required for races, not a second incompatible estimation authority.
- FIX-04: `_quota_rank` and `_quota_rank_sync_local` still sum raw soft shortages
  and compare raw minimum remaining. Shared normalization and max dimension
  pressure must replace these. Current E2E-03 supplies 0.95 directly to metrics;
  it does not prove RouterEngine computes multidimensional pressure.
- FIX-05: Full requested model is not present: distinct reserved,
  window_start/window_end and replenish_rate are absent from current domain/DB.
  Metadata serialization fix does not add those columns or imply their gate is
  complete. PostgreSQL round trip remains unproven here.
- FIX-06: No reset transition currently exists in quota authority or runtime
  index. Policies fixed_timestamp, fixed_window, provider_managed and none need
  explicit semantics; active reservation validity and reconciliation across
  windows must be handled atomically. Shared/parent reset, multi-client races,
  background refresh and real restart remain unproven. Startup DB hydration
  currently overwrites Redis counters, another lifecycle risk.

## Verification

`python -m pytest tests/unit/test_quota_reset_metadata.py tests/unit/test_pr08_quota_schema.py tests/unit/test_pr09_runtime_quota_index.py tests/unit/test_router_engine_quota_graph.py tests/unit/test_redis_quotas.py -q`

Result: 28 passed, 6 skipped (live Redis unavailable). New metadata tests use
FakeServer and distinct FakeAsyncRedis clients and do not skip. TDD RED observed
KeyError: 'reset_at'; GREEN: both metadata tests passed.

Scope: fix/p0-quota worktree only; no push or merge. No claim that FIX-03 through
FIX-06 or P0 is complete.
