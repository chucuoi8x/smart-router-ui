# Smart Router — P0 Final Convergence Implementation Plan

> **Purpose:** Mandatory implementation plan for finishing Smart Router P0 stabilization before continuing the main roadmap in `README.md`.
>
> **Audience:** AI coding agents and maintainers.
>
> **Priority:** BLOCKING / P0.
>
> **Rule:** Do not add new providers, advanced optimizer features, forecasting, plugin marketplace features, multimodal expansion, major UI redesign, or other roadmap features until this document is complete.

---

# 1. Objective

The current codebase has already improved significantly and now contains most of the architectural building blocks required by the project:

- new `RouterEngine`
- canonical resource identity including credential
- PostgreSQL-backed Provider Registry
- real Generic Provider Drivers
- Redis-backed quota reservation
- Runtime Quota Index
- SSRF protection
- production secret checks
- CI scaffolding
- Control Plane persistence
- config revision persistence
- streaming safeguards
- worker scaffolding

However, several of these components are still not connected into one authoritative end-to-end runtime.

The goal of this plan is to close the remaining gaps so that the actual production request flow becomes:

```text
Control Plane
    ↓
PostgreSQL
    ↓
Active Config Revision
    ↓
Runtime Config Compiler
    ↓
Immutable Runtime Snapshot
    ↓
Schedulable Resources
    ↓
Capability + Quota Eligibility
    ↓
Smart Scheduler
    ↓
Atomic Quota Reservation
    ↓
Provider Driver
    ↓
Provider
    ↓
Usage / Failure Events
    ↓
Worker
    ↓
PostgreSQL + Redis Reconciliation
```

P0 is complete only when this chain is real, persistent across restart, works without manual config file edits, and passes mandatory E2E tests.

---

# 2. Final P0 principles

All implementation decisions must follow these principles.

## 2.1 One runtime authority

There must be exactly one production routing path.

All supported protocols must ultimately use the same `RouterEngine`.

Do not reintroduce:

```text
legacy router path
vs
new router path
```

Legacy configuration may continue to be accepted, but only through a compatibility compiler that produces the same new runtime model.

---

## 2.2 PostgreSQL is durable configuration truth

The following must come from PostgreSQL after migration:

```text
provider definitions
provider connections
provider credentials
provider models
model resources
routes
route candidates
routing policies
projects
budgets
config revisions
audit state
durable quota metadata
```

`config.yaml` may be used only for:

```text
initial bootstrap
legacy migration
development compatibility
```

It must not remain the production runtime authority after an active DB revision exists.

---

## 2.3 Redis is distributed transient truth

Use Redis for:

```text
atomic quota reservation
cooldowns
distributed circuit state
concurrency
session bindings when required
revision notifications
runtime invalidation
event stream / usage queue
```

Do not use Redis as a relational database queried globally on every request.

---

## 2.4 In-memory state is cache only

Process memory may contain:

```text
immutable RuntimeSnapshot
compiled route index
capability index
runtime quota read cache
local metrics cache
HTTP client registry
```

It must not be the only source of durable truth.

---

## 2.5 Resource identity includes credential

The canonical scheduling identity is:

```text
connection_id
+
credential_id
+
model_id
```

This identity must be used consistently everywhere.

---

# 3. Implementation order

AI agents must follow this order unless a dependency forces a small adjustment.

```text
PHASE 1
Runtime authority + Config activation

PHASE 2
Provider onboarding E2E + Credential model

PHASE 3
Quota correctness inside RouterEngine

PHASE 4
Async request path + Usage pipeline + Streaming

PHASE 5
Distributed runtime + Worker completion

PHASE 6
Validation, readiness, CI, E2E gate
```

Do not skip directly to later phases.

---

# 4. PHASE 1 — Make Active Config Revision control the real runtime

This is the highest-priority blocker.

---

## 4.1 Current problem

The application still boots primarily from:

```text
config.yaml
    ↓
SmartRouter.from_environment()
    ↓
RuntimeSnapshot
```

Admin config activation persists DB state, but activation does not yet guarantee:

```text
DB active revision
=
actual runtime revision
```

This means a provider or route may exist in the Control Plane while real traffic still uses an older runtime snapshot.

---

## 4.2 Required target

Create a single authoritative component:

```text
RuntimeConfigManager
```

Responsibilities:

```text
load active revision
validate revision
compile RuntimeSnapshot
create/reuse provider clients
atomically activate snapshot
publish revision notification
expose current runtime revision
support rollback
```

Suggested conceptual interface:

```python
class RuntimeConfigManager:
    async def load_initial(self) -> None:
        ...

    async def activate(self, revision_id: str) -> RuntimeSnapshot:
        ...

    async def rollback(self, revision_id: str) -> RuntimeSnapshot:
        ...

    @property
    def snapshot(self) -> RuntimeSnapshot:
        ...

    @property
    def active_revision_id(self) -> str:
        ...
```

---

## 4.3 Startup behavior

Implement startup in this order:

```text
Initialize database
        ↓
Initialize Redis
        ↓
Load active config revision from PostgreSQL
        │
        ├─ revision exists
        │      ↓
        │   compile revision
        │
        └─ no active revision
               ↓
         migrate legacy config.yaml
               ↓
         create initial revision
               ↓
         activate revision
        ↓
Create RuntimeConfigManager
        ↓
Hydrate RuntimeQuotaIndex
        ↓
Start workers
        ↓
Ready
```

After migration, `config.yaml` must not override an active DB revision.

---

## 4.4 Config activation

`POST activate revision` must execute:

```text
load revision
↓
schema validation
↓
semantic validation
↓
compile resources
↓
compile routes
↓
compile quota bindings
↓
create HTTP client bindings
↓
atomic snapshot swap
↓
persist active revision
↓
publish Redis notification
```

If any step fails:

```text
old runtime snapshot remains active
```

No partial activation is allowed.

---

## 4.5 Runtime snapshot structure

The runtime snapshot must contain enough information so `router.py` does not need to infer providers.

Suggested shape:

```python
RuntimeSnapshot(
    revision_id=...,
    provider_definitions=...,
    provider_connections=...,
    credentials=...,
    model_resources=...,
    routes=...,
    routing_policies=...,
    quota_bindings=...,
    client_bindings=...,
)
```

The snapshot should be immutable after compilation.

---

## 4.6 Remove runtime driver guessing

The current router still contains some logic that infers driver type from upstream names or protocol hints.

Remove this.

Each compiled `SchedulableResource` must already include:

```text
driver_id
connection_id
credential_id
model_id
protocol
```

Router runtime must execute the resource exactly as compiled.

---

## 4.7 Fix strategy validation

Current validator does not accept all strategies used by the actual config.

For example:

```text
smooth_weighted_rr
```

must be recognized if supported by RouterEngine.

Do not maintain duplicate strategy lists.

Create one authoritative registry:

```python
SUPPORTED_ROUTING_STRATEGIES = {...}
```

used by:

```text
RouterEngine
Config validator
Control Plane UI schema
tests
```

---

## 4.8 Phase 1 acceptance criteria

- [ ] App boots from active DB revision.
- [ ] Existing `config.yaml` is migrated only when no active revision exists.
- [ ] Runtime exposes active revision ID.
- [ ] Runtime revision equals DB active revision.
- [ ] Activating a revision changes real request routing without restart.
- [ ] Invalid revision does not replace current runtime.
- [ ] Rollback restores prior runtime behavior.
- [ ] `smooth_weighted_rr` and every supported strategy validate correctly.
- [ ] Router no longer guesses driver type.

---

# 5. PHASE 2 — Make dynamic provider onboarding real end-to-end

This phase closes the largest gap between Control Plane and Data Plane.

---

## 5.1 Current problem

The project can persist providers and models, but current tests mainly verify Control Plane state.

The system must prove:

```text
Add Provider
→ Activate
→ Real request uses provider
```

without restart.

---

## 5.2 Fix Provider Test endpoint semantics

Current behavior may return:

```json
{
  "ok": true
}
```

even if the driver result reports an error.

This must be fixed.

### Required response logic

If driver returns:

```text
status = ok
```

then:

```json
{
  "ok": true
}
```

If driver returns:

```text
status = error
```

then:

```json
{
  "ok": false,
  "error_kind": "...",
  "message": "..."
}
```

Use meaningful HTTP status where appropriate.

Examples:

```text
bad config        → 400
auth rejected     → 401 / 403
network/upstream  → 502
timeout           → 504
```

Do not report false success.

---

## 5.3 Build one DriverContext factory

Currently Test, Discover, Execute, and Quota paths can construct context differently.

Create one central builder.

Suggested:

```python
async def build_driver_context(
    connection_id: str,
    credential_id: str | None = None,
) -> DriverContext:
    ...
```

It must resolve:

```text
connection
selected credential
decrypted secret
base_url
driver_id
headers
timeout
proxy
TLS options
private-network policy
metadata
```

All provider operations must use this builder:

```text
Test
Discover
Execute
Stream
Fetch Quota
Health Check
Catalog Sync
```

---

## 5.4 Fix model discovery authentication

`discover_models()` must receive the same credential context required by the provider.

Errors must not silently become:

```text
[]
```

unless provider genuinely has zero models.

Return structured errors such as:

```text
AUTH_FAILED
NETWORK_ERROR
UNSUPPORTED_DISCOVERY
INVALID_RESPONSE
```

---

## 5.5 Remove new writes to legacy credential column

When creating a provider with an initial API key:

Do not store the new key only in:

```text
ProviderConnection.credential_encrypted
```

Instead create:

```text
ProviderConnection
    +
ProviderCredential(alias="default")
```

Legacy connection-level credential storage may remain readable only for migration.

---

## 5.6 Materialize real SchedulableResources

The runtime compiler must expand:

```text
Connection
×
Enabled Credentials
×
Enabled Models
```

into:

```text
SchedulableResource
```

Example:

```text
Connection G
Credentials:
  A
  B

Model:
  llama-x
```

must produce:

```text
G:A:llama-x
G:B:llama-x
```

Each resource carries independent:

```text
quota
health
circuit
latency
usage
session state
```

---

## 5.7 Provider onboarding E2E test

Create a mock OpenAI-compatible provider.

The test must perform:

```text
Start Smart Router with empty DB
↓
Create provider
↓
Create credential
↓
Test connection
↓
Discover models
↓
Import model
↓
Create route
↓
Activate revision
↓
POST /v1/chat/completions
↓
Mock provider receives request
↓
Response returned
```

Verify:

```text
request ledger exists
attempt ledger exists
usage event exists
correct resource key recorded
```

Then:

```text
restart application
```

and send the request again.

It must still work.

---

## 5.8 Credential isolation E2E test

Setup:

```text
Connection X
 ├─ Credential A
 │    └─ Model M
 └─ Credential B
      └─ Model M
```

Make A return:

```text
401
```

Expected:

```text
A disabled/cooldown according to failure policy
B remains eligible
next request uses B
```

B must never be poisoned by A.

---

## 5.9 Phase 2 acceptance criteria

- [ ] Test endpoint reports real success/failure.
- [ ] Discover uses credentials.
- [ ] Discover reports structured errors.
- [ ] New credentials are always `ProviderCredential` records.
- [ ] Runtime compiler expands credentials × models.
- [ ] Dynamic provider routes real traffic without restart.
- [ ] Dynamic provider survives restart.
- [ ] Credential isolation test passes.
- [ ] Control Plane state and Data Plane behavior are consistent.

---

# 6. PHASE 3 — Move complete quota eligibility into RouterEngine

---

## 6.1 Current problem

RouterEngine currently evaluates some quota dimensions, especially request-based limits, while other dimensions may be enforced later during reservation.

This causes duplicated eligibility logic.

Example:

```text
RouterEngine says candidate eligible
↓
later token reservation fails
```

This is architecturally incorrect.

---

## 6.2 Required target

RouterEngine must own complete candidate eligibility.

For each resource:

```text
collect applicable quota resources
↓
estimate required amount per metric
↓
evaluate all hard constraints
↓
calculate normalized quota pressure
↓
filter ineligible resources
↓
score eligible resources
↓
atomic reserve chosen resource
```

---

## 6.3 Supported quota dimensions

The design must support, without hardcoding provider names:

```text
requests
input_tokens
output_tokens
total_tokens
credits
USD
concurrency
compute units
provider-defined units
```

---

## 6.4 Never mix native units

Do not compare:

```text
50 requests
20000 tokens
5 USD
```

directly.

Normalize each quota independently.

Example:

```text
RPM remaining ratio = 0.80
TPM remaining ratio = 0.05
Daily remaining ratio = 0.70
```

Then:

```text
effective_headroom = min(0.80, 0.05, 0.70)
                   = 0.05
```

or:

```text
pressure = max(
    1 - remaining_ratio
)
```

No raw-unit sum/min/max is permitted.

---

## 6.5 Required quota metadata

Ensure the same fields survive:

```text
PostgreSQL
→ Runtime model
→ Redis
→ RuntimeQuotaIndex
```

Required fields:

```text
id
scope
metric
limit
used
reserved
remaining
safety_buffer
hard_limit
shared_group_id
parent_id
window_seconds
window_start
window_end
reset_at
window_metadata
replenish_rate
source
confidence
```

Not every field must be non-null.

---

## 6.6 Fix Redis serialization

Ensure Redis stores and restores:

```text
reset_at
window_metadata
```

and every field needed by optimizer/runtime.

No information required by scheduling may disappear during DB → Redis hydration.

---

## 6.7 Implement reset lifecycle

Implement provider-independent reset handling.

At minimum support:

```text
fixed reset timestamp
fixed duration window
manual/provider-reported reset
non-resetting credit pool
```

When:

```text
now >= reset_at
```

the quota state must transition correctly.

Do not blindly reset quota types that are not periodic.

---

## 6.8 RuntimeQuotaIndex

Use local index for read-heavy scheduling.

Routing path:

```text
RuntimeQuotaIndex
    ↓
candidate evaluation
```

Redis is used for:

```text
atomic reservation
reconciliation
distributed update
```

Do not reintroduce global Redis scans.

---

## 6.9 Burn-rate naming correctness

Keep separate metrics:

```text
quota_pressure
burn_rate
expiry_urgency
```

Definitions:

```text
quota_pressure =
1 - remaining / effective_limit
```

```text
burn_rate =
recent_consumption / time
```

```text
target_burn_rate =
effective_remaining / time_until_reset
```

Do not use the term burn-rate for pressure ratio.

---

## 6.10 Phase 3 tests

Mandatory:

### Multi-dimensional quota

```text
RPM remaining = 90%
TPM remaining = 5%
Daily remaining = 80%
```

Expected:

```text
effective headroom ≈ 5%
```

### Shared quota

Two model resources share one daily quota.

Usage by Model A must reduce availability for Model B.

### Parent quota

Child resources must not exceed parent quota.

### Concurrent reservation

Given:

```text
100000 tokens available
```

parallel reservations:

```text
40000
40000
35000
```

must permit only the first two.

### Reset behavior

Quota must reset only according to its configured reset semantics.

---

## 6.11 Phase 3 acceptance criteria

- [ ] RouterEngine evaluates every hard quota dimension.
- [ ] No later hidden quota eligibility layer contradicts RouterEngine.
- [ ] Native units are normalized before ranking.
- [ ] Redis preserves reset/window metadata.
- [ ] Parent/shared quota survives restart.
- [ ] Concurrent reservation never oversubscribes.
- [ ] Reset behavior is tested.

---

# 7. PHASE 4 — Remove blocking I/O from request path

This is required before Smart Scheduler is enabled broadly.

---

## 7.1 Remove sync-to-async thread bridges

Current request path still contains patterns similar to:

```text
thread.start()
thread.join(...)
```

to call async operations from sync code.

This must be removed from request processing.

### Required rule

Async request path remains async end-to-end.

Use:

```python
await repository.operation()
```

not:

```text
async
→ thread
→ new event loop
→ join
```

---

## 7.2 Smart scoring must use local state

Scoring must not fetch Redis once per candidate.

Use:

```text
RuntimeQuotaIndex
local metrics snapshot
compiled resource metadata
```

Redis mutation should happen at reservation/cooldown boundaries.

---

## 7.3 Convert circuit operations to async

Request path should use:

```python
await circuit_repository.trip(...)
await circuit_repository.clear(...)
```

or equivalent async APIs.

Do not invoke Redis via synchronous wrappers from async code.

---

# 8. PHASE 4B — Implement real asynchronous Usage Ledger

---

## 8.1 Current problem

Usage repository operations still perform:

```text
session.add
flush
```

while request handling awaits them.

This makes PostgreSQL latency part of the critical request path.

---

## 8.2 Required design

Use an event pipeline.

Preferred:

```text
Gateway
    ↓
UsageEvent
    ↓
Redis Stream
    ↓
UsagePersistenceWorker
    ↓
batch insert
    ↓
PostgreSQL
```

Minimum events:

```text
RequestStarted
AttemptStarted
AttemptFailed
FirstToken
AttemptSucceeded
UsageObserved
QuotaReserved
QuotaReconciled
RequestCompleted
```

---

## 8.3 Request-path guarantee

Analytics writes must not block request completion.

The request path may synchronously persist only state needed for correctness.

Normal telemetry should be queued.

---

## 8.4 Worker batching

Worker should:

```text
read batch
insert batch
ack batch
retry failure
```

Support:

```text
idempotency
event IDs
retry policy
dead-letter handling or failure logging
```

---

# 9. PHASE 4C — Wire incremental SSE UsageTap into real streaming path

---

## 9.1 Current state

Incremental parser implementation already exists, but the main streaming path still maintains a limited tail buffer.

Memory safety is better than before, but usage accounting may lose early stream metadata.

---

## 9.2 Required target

Use:

```text
upstream chunk
   ├────────→ downstream client immediately
   │
   └────────→ IncrementalSSEUsageParser
                     ↓
                  UsageTap
```

UsageTap stores only:

```text
partial SSE frame
input token count
output token count
cached token count
reasoning token count
provider usage metadata
```

Do not keep the response body.

---

## 9.3 Preserve first-token invariant

Mandatory:

```text
Before first downstream token:
failover may occur.

After first downstream token:
no provider/model switch.
```

Do not weaken this invariant.

---

## 9.4 Streaming tests

Test:

```text
large SSE stream
input usage in first event
output usage in final event
stream > 64 KB
```

Expected:

```text
input usage retained
output usage retained
memory stays bounded
```

---

# 10. PHASE 5 — Complete distributed runtime state

---

## 10.1 Distributed circuit state

Already improved, but verify:

```text
Gateway A
trips Credential X
```

then:

```text
Gateway B
stops scheduling Credential X
```

within expected propagation time.

---

## 10.2 Session affinity

Decide explicitly:

### If cross-instance affinity is required

Store binding in Redis.

### If not required in P0

Document that affinity is process-local and not a correctness dependency.

Do not leave ambiguous behavior.

---

## 10.3 Active revision distribution

When one gateway activates revision:

```text
Redis publish revision_id
```

all gateways:

```text
load revision
validate hash/version
compile
atomic swap
```

If a gateway fails compilation:

```text
do not silently serve mixed revisions
```

Expose degraded readiness.

---

# 11. PHASE 5B — Make worker real

Current worker classes exist but runtime must execute them.

Minimum worker responsibilities for P0:

```text
usage event persistence
quota synchronization
stale reservation cleanup
provider catalog sync framework
```

Optional after P0:

```text
pricing snapshots
metrics aggregation
alert evaluation
forecast preparation
```

---

## 11.1 Stale reservation cleanup

If request crashes after reservation:

```text
reserved quota
```

must eventually be released or reconciled.

Use:

```text
reservation TTL
worker cleanup
idempotent reconciliation
```

---

# 12. PHASE 6 — Failure scope correctness

---

## 12.1 Replace ambiguous scope strings

Do not use composite strings such as:

```text
credential/model/connection
request/provider
```

Create a first-class enum:

```python
class FailureScope(Enum):
    REQUEST = "request"
    MODEL = "model"
    CREDENTIAL = "credential"
    CONNECTION = "connection"
    PROVIDER = "provider"
    QUOTA_RESOURCE = "quota_resource"
```

---

## 12.2 FailureDecision

Use structured output:

```python
FailureDecision(
    kind=...,
    scope=...,
    scope_id=...,
    retryable=...,
    disable=...,
    cooldown_until=...,
)
```

Examples:

### Bad request

```text
INVALID_REQUEST
scope=REQUEST
retryable=false
```

### Bad API key

```text
AUTH_REVOKED
scope=CREDENTIAL
scope_id=cred_123
```

### TPM exceeded

```text
RATE_LIMIT
scope=QUOTA_RESOURCE
scope_id=tpm:cred_123:model_x
```

### Model overload

```text
MODEL_OVERLOADED
scope=MODEL
scope_id=model_x
```

Runtime state mutation must use exact scope.

---

# 13. PHASE 6B — Separate capability and quality

---

## 13.1 Capability

Capability answers:

```text
Can this resource satisfy the request?
```

Examples:

```text
tools
vision
streaming
structured output
context length
reasoning mode
```

Primarily used for eligibility.

---

## 13.2 Quality

Quality answers:

```text
How suitable is this model for this workload?
```

Add:

```python
quality_score: float
```

to `CandidateMetrics`.

Add:

```python
quality_factor
```

to scoring weights.

Do not map:

```text
quality_fit
```

onto:

```text
capability_factor
```

---

# 14. PHASE 6C — Fix readiness

---

## 14.1 Liveness

```text
/health/live
```

only checks process health.

---

## 14.2 Readiness

```text
/health/ready
```

must verify:

```text
PostgreSQL SELECT 1
Redis PING
active revision exists
runtime snapshot exists
runtime revision == DB active revision
encryption key requirement satisfied
worker/event dependencies available if mandatory
```

---

## 14.3 Do not leak DB connections

Use:

```python
async with engine.connect() as conn:
    await conn.execute(...)
```

or equivalent.

Do not call connect repeatedly without close.

---

## 14.4 Readiness response

Expose:

```json
{
  "status": "ok",
  "db_revision": "rev_123",
  "runtime_revision": "rev_123",
  "redis": "ok",
  "database": "ok"
}
```

Mismatch should report:

```text
degraded/unready
```

---

# 15. CI and test gate

Existing CI is an improvement, but add mandatory P0 jobs.

Required:

```text
lint
schema/type check
unit
integration
migration
Docker build
P0 E2E
quota concurrency
distributed state
```

Recommended nightly:

```text
load
chaos
long streaming
```

---

# 16. Mandatory final E2E suite

P0 may not close until all tests below pass.

---

## E2E-01 — Dynamic provider onboarding

```text
empty DB
→ create provider
→ create credential
→ test
→ discover
→ import model
→ create route
→ activate
→ chat request
→ provider receives request
```

Then restart.

Request must still succeed.

---

## E2E-02 — Credential isolation

```text
Credential A → 401
Credential B → healthy
```

Expected:

```text
A disabled
B remains selectable
```

---

## E2E-03 — Config activation

```text
revision A active
→ request uses A

activate revision B
→ next request uses B

no restart
```

---

## E2E-04 — Invalid revision safety

```text
revision A active
→ attempt invalid B
→ activation fails
→ A remains active
```

---

## E2E-05 — Rollback

```text
A
→ B
→ rollback A
```

Requests must follow the active revision each time.

---

## E2E-06 — Multi-dimensional quota

```text
RPM = 90% remaining
TPM = 5% remaining
Daily = 80% remaining
```

Expected:

```text
effective quota headroom = TPM constraint
```

---

## E2E-07 — Concurrent reservation

```text
available=100000

R1=40000
R2=40000
R3=35000
```

Only two may reserve this resource.

---

## E2E-08 — Cross-instance circuit

Gateway A trips credential.

Gateway B must stop scheduling it.

---

## E2E-09 — Streaming usage

Large stream:

```text
input usage in first event
output usage in final event
```

All token values must be recorded correctly.

---

## E2E-10 — Async ledger resilience

Artificially slow PostgreSQL.

Streaming response should not wait on normal analytics flush.

Queued usage eventually persists.

---

# 17. Performance requirements

Do not optimize Python scoring prematurely.

Current arithmetic performance is sufficient.

Focus on I/O architecture.

Targets after warmup:

```text
candidate filtering + scoring:
p95 < 2 ms

no global Redis scan per request

Redis quota reservation:
single atomic round-trip where possible

analytics DB writes on request path:
0 normal synchronous writes

stream parsing:
bounded memory

no thread.join in async request path
```

---

# 18. Files/modules AI should inspect first

Exact paths may differ slightly as refactoring progresses, but start with:

```text
router.py

apps/gateway/routing/engine.py
apps/gateway/routing/models.py
apps/gateway/routing/scoring.py
apps/gateway/routing/circuit.py

apps/gateway/config/
apps/gateway/runtime/

apps/gateway/providers/
apps/gateway/drivers/

apps/gateway/quota/
apps/gateway/usage/

apps/gateway/api/admin.py
apps/gateway/api/health.py

apps/gateway/db/
apps/worker/

tests/integration/
tests/e2e/
tests/acceptance/
```

Do not create duplicate replacements without first checking existing abstractions.

---

# 19. Recommended PR sequence

Use small, dependency-aware PRs.

---

## PR-01 — RuntimeConfigManager

Deliver:

```text
active revision loader
runtime snapshot compiler
atomic swap
runtime revision visibility
```

---

## PR-02 — Startup migration

Deliver:

```text
DB active revision preferred
legacy config migration only when needed
```

---

## PR-03 — Real config activation + rollback

Deliver:

```text
validate
compile
activate
rollback
Redis notification
```

---

## PR-04 — DriverContext factory

Deliver:

```text
shared context for test/discover/execute/quota
```

Fix provider test result handling.

Fix discovery authentication.

---

## PR-05 — Credential normalization

Deliver:

```text
all new credentials use ProviderCredential
legacy credential read-only migration
```

---

## PR-06 — SchedulableResource materialization

Deliver:

```text
Connection × Credentials × Models
```

and runtime compiler integration.

---

## PR-07 — Dynamic provider E2E

Deliver:

```text
Add → Activate → Chat → Restart → Chat
```

---

## PR-08 — Quota eligibility convergence

Move all quota hard-constraint evaluation into RouterEngine.

Normalize metrics.

---

## PR-09 — Redis quota metadata completeness

Persist and hydrate:

```text
reset_at
window metadata
parent/shared metadata
```

Implement reset semantics.

---

## PR-10 — Async-only routing path

Remove thread joins and sync bridges.

---

## PR-11 — Usage event pipeline

Redis Stream / queue + worker batch persistence.

---

## PR-12 — Incremental UsageTap

Wire existing incremental SSE parser into main streaming path.

---

## PR-13 — Distributed state verification

Cross-instance circuit and revision propagation.

---

## PR-14 — FailureScope + quality scoring

Formalize scope.

Separate quality from capability.

---

## PR-15 — Readiness + final P0 E2E

Complete readiness checks and run full gate.

---

# 20. Definition of P0 complete

Every checkbox must be true.

## Runtime

- [ ] One RouterEngine is authoritative.
- [ ] Active PostgreSQL revision controls real runtime.
- [ ] `config.yaml` is only bootstrap/legacy migration after DB activation.
- [ ] Runtime revision ID is observable.
- [ ] Config activation requires no restart.
- [ ] Rollback works.

## Providers

- [ ] Provider test performs real validation.
- [ ] Provider discovery uses credential.
- [ ] Discovery errors are structured.
- [ ] New credentials always use `ProviderCredential`.
- [ ] Connection × credentials × models becomes real resources.
- [ ] Dynamic provider E2E passes.
- [ ] Restart persistence E2E passes.

## Quota

- [ ] RouterEngine evaluates all hard quota constraints.
- [ ] Native units are never directly mixed.
- [ ] Parent/shared quota survives persistence.
- [ ] Redis preserves reset metadata.
- [ ] Reset lifecycle works.
- [ ] Concurrent reservation test passes.

## Async/runtime

- [ ] No thread join in request path.
- [ ] No per-candidate Redis network fetch for normal scoring.
- [ ] Usage telemetry does not synchronously flush analytics to DB.
- [ ] Worker persists usage events.
- [ ] Streaming usage parser is incremental.
- [ ] Large stream keeps bounded memory.

## Distributed state

- [ ] Credential cooldown propagates cross-instance.
- [ ] Active revision propagates cross-instance.
- [ ] Resource identity always includes credential.

## Correctness

- [ ] FailureScope is explicit.
- [ ] Capability and quality are separate.
- [ ] Invalid config cannot replace last-known-good.
- [ ] Current production config validates successfully.

## Operations

- [ ] `/health/live` correct.
- [ ] `/health/ready` verifies DB, Redis, and revision convergence.
- [ ] No readiness DB connection leak.
- [ ] production encryption key remains mandatory.
- [ ] SSRF protections remain enabled.
- [ ] CI runs P0 E2E tests.
- [ ] Clean checkout CI passes.

---

# 21. AI implementation rules

These rules are mandatory.

---

## 21.1 Do not solve failures with provider-specific conditionals

Forbidden outside drivers/templates:

```python
if provider == "aibox":
    ...
elif provider == "groq":
    ...
```

---

## 21.2 Do not create another runtime path

If a feature does not fit the existing RouterEngine:

```text
improve RouterEngine
```

Do not create:

```text
RouterEngineV2
AlternativeRouter
TemporarySmartRouter
```

---

## 21.3 Do not create a new source of truth

Classify state:

```text
durable
→ PostgreSQL

distributed transient
→ Redis

read-only/cache
→ memory
```

---

## 21.4 Do not call a feature complete based only on Admin API state

Every feature affecting routing must be proven by a Data Plane request.

---

## 21.5 Prefer E2E proof over mocks of the implementation itself

Mocks should represent upstream providers and infrastructure boundaries.

Do not mock away:

```text
runtime compiler
RouterEngine
quota reservation
driver execution
config activation
```

in the final acceptance tests.

---

## 21.6 Preserve backward compatibility via compilation

Legacy config support is allowed.

Legacy runtime is not.

---

## 21.7 Do not continue README roadmap early

The main roadmap may resume only when:

```text
all P0 Definition of Complete checks pass
AND
mandatory E2E suite passes
AND
clean CI passes
```

---

# 22. Final target architecture

After this plan is complete:

```text
                             CLIENT
                               │
                               ▼
                    Universal API Gateway
                               │
                               ▼
                     Canonical Request
                               │
                               ▼
                    Capability Resolver
                               │
                               ▼
                   Schedulable Resources
                               │
                               ▼
                  Universal Quota Graph
                               │
                               ▼
                    Smart RouterEngine
                               │
                               ▼
                 Atomic Quota Reservation
                               │
                               ▼
                      Provider Driver
                               │
                               ▼
                         Provider
                               │
                               ▼
                   Usage / Failure Events
                               │
                 ┌─────────────┴─────────────┐
                 ▼                           ▼
               Redis                       Worker
       distributed runtime state              │
                                               ▼
                                          PostgreSQL
                                          durable truth
```

The purpose of this stabilization cycle is not feature expansion.

The purpose is to make this architecture **actually true at runtime**.

Only after this is complete should development continue with:

```text
free-capacity optimizer
real burn-rate scheduling
scarcity model
expected retry economics
forecasting
token optimization plane
additional provider templates
advanced Control Plane UX
```
