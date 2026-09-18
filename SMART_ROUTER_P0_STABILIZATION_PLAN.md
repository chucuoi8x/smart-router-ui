# Smart Router — P0 Stabilization & Architecture Convergence Plan

> **Purpose:** This document is a mandatory stabilization gate before continuing the implementation roadmap in `README.md`.
>
> **Audience:** AI coding agents, maintainers, reviewers, and contributors working on Smart Router.
>
> **Status:** P0 / blocking.
>
> **Rule:** Do **not** continue adding providers, advanced optimizer features, forecasting, plugin marketplace features, or broader UI scope until all P0 acceptance criteria in this document pass.

---

## 1. Why this document exists

The current Smart Router codebase is moving in the correct architectural direction, but it currently contains two partially overlapping generations of the system:

1. the legacy router/runtime, which is still the effective default path; and
2. the newer architecture, which introduces Provider Registry, Quota Engine, RouterEngine, resource abstractions, usage accounting, and Control Plane concepts.

The project must now converge these two worlds into a single runtime architecture.

The immediate goal is **not** to add more features. The immediate goal is to ensure that the core runtime matches the architecture defined in `README.md` and that all critical state transitions are correct, persistent, deterministic, and testable.

The codebase must not move forward until the following chain is real and end-to-end:

```text
Control Plane
    ↓
PostgreSQL
    ↓
Config Revision / Compiler
    ↓
Runtime Snapshot
    ↓
SchedulableResource
    ↓
Quota Graph
    ↓
Smart Scheduler
    ↓
Provider Driver
    ↓
Provider
    ↓
Usage Event
    ↓
Quota Reconciliation
```

---

# 2. Current implementation status

The project already contains several strong building blocks:

- FastAPI-based async gateway
- OpenAI and Anthropic compatibility layers
- streaming support
- first-token failover invariant
- `httpx.AsyncClient` connection reuse
- circuit breaker concepts
- provider driver abstraction
- CLIProxy bridge
- quota resource model
- Redis-backed atomic quota reservation
- usage ledger structures
- error classification
- retry budgets
- Smart Scheduler scaffolding
- Control Plane API scaffolding
- significant automated test coverage

However, several major architectural features still exist only as scaffolding or in-memory prototypes.

The largest risk is that the code can appear structurally complete while the real request path still follows legacy behavior.

---

# 3. P0 architecture invariants

The following invariants are mandatory.

## 3.1 Only one router runtime may exist

There must be one authoritative runtime path.

The final runtime must be:

```text
Request
  ↓
Protocol Adapter
  ↓
Canonical Request Metadata
  ↓
Capability Resolver
  ↓
Schedulable Resource Resolver
  ↓
Quota / Health / Policy Filter
  ↓
Smart Scheduler
  ↓
Atomic Reservation
  ↓
Provider Driver
  ↓
Provider
  ↓
Usage / Failure / Quota Reconciliation
```

Legacy routing logic must not remain as an alternative production path.

A compatibility layer may translate old configuration into the new resource model, but the compatibility layer must feed the same RouterEngine.

---

## 3.2 Router core must be provider-agnostic

The core router must not contain provider names such as:

```text
aibox
xkiro
proxypal
groq
gemini
openrouter
```

Provider-specific behavior must live in one of the following:

- provider template
- provider driver
- provider plugin
- catalog collector
- quota collector
- worker task
- CLIProxy bridge

The routing engine must operate only on normalized runtime resources.

---

## 3.3 A schedulable resource is the real routing unit

The scheduling identity must include:

```text
ProviderConnection
+
Credential
+
Model
```

Conceptually:

```text
SchedulableResource =
    connection_id
    credential_id
    model_id
```

Two credentials for the same provider and same model are two different resources.

This identity must be used consistently by:

- circuit breaker
- quota tracking
- health tracking
- latency tracking
- session affinity
- usage ledger
- retry logic
- scheduler
- metrics

Do not collapse state to only:

```text
connection_id:model_id
```

because that incorrectly couples multiple credentials.

---

## 3.4 PostgreSQL is durable truth

The following must not rely on process-local dictionaries:

- provider definitions
- provider connections
- credentials
- model resources
- routes
- policies
- projects
- budgets
- alerts
- config revisions
- audit events
- durable quota state
- usage ledger

In-memory state may only be used as a cache or immutable runtime snapshot.

---

## 3.5 Redis is transient distributed truth

Redis should hold runtime state such as:

- atomic quota reservations
- distributed circuit state
- short-lived cooldowns
- concurrency counters
- session affinity
- transient rate-limit state
- event queues / streams
- config invalidation or revision notifications

Redis must not be treated as the main relational query engine for every request.

---

## 3.6 Configuration changes must be atomic

A runtime configuration update must follow:

```text
Create revision
    ↓
Validate
    ↓
Compile
    ↓
Persist
    ↓
Atomically activate
    ↓
Notify gateway instances
    ↓
Swap immutable runtime snapshot
```

If validation or compilation fails, the currently active revision must remain unchanged.

The system must support a last-known-good configuration.

---

# 4. P0-01 — Eliminate the dual-router architecture

## Problem

The current code still allows the legacy router to remain the default path while the newer `RouterEngine` is enabled only through feature flags.

This means the codebase can contain the new architecture without actually using it for real traffic.

## Impact

- README architecture does not match runtime behavior.
- new provider registry logic can exist without affecting requests.
- quota-aware scheduling may never run.
- legacy provider-specific logic remains authoritative.
- future contributors may unknowingly maintain two routing engines.

## Required fix

### Step 1

Make the new RouterEngine the only production routing engine.

### Step 2

Convert legacy configuration into the new runtime model through a compatibility compiler.

Example:

```text
legacy config.yaml
      ↓
LegacyConfigAdapter
      ↓
ProviderConnection
Credential
ModelResource
Route
Policy
      ↓
RuntimeSnapshot
```

### Step 3

Remove direct legacy scheduling decisions from `router.py`.

`router.py` should become primarily:

```text
HTTP handling
protocol adaptation
request context construction
RouterEngine invocation
stream forwarding
error mapping
```

### Step 4

Remove or deprecate:

```text
USE_ROUTER_ENGINE
```

as a production toggle.

The new engine must be the default.

## Acceptance criteria

- all OpenAI and Anthropic requests flow through the same RouterEngine;
- disabling the legacy compatibility layer does not remove the new routing path;
- no request can bypass quota/resource evaluation by falling into the old router;
- integration tests verify the RouterEngine is invoked for all supported API protocols.

---

# 5. P0-02 — Remove provider-specific behavior from router core

## Problem

AI-BOX, xKiro, ProxyPal, catalog sync, and provider-specific fallback logic are still directly referenced from core routing code or core API paths.

## Impact

This breaks the main architectural rule:

> Router core must not know the identities of upstream providers.

If this remains, every new provider will eventually add new conditional branches and the system will become another hardcoded router.

## Required fix

Move responsibilities as follows:

```text
AI-BOX catalog discovery
    → provider catalog collector / worker

AI-BOX model promotion logic
    → provider template metadata + generic policy layer

xKiro-specific request behavior
    → provider driver or plugin

ProxyPal
    → compatibility / migration only
      or CLIProxy bridge

provider sync endpoint
    → generic:
      /providers/{id}/sync
```

Core runtime APIs should become generic.

For example:

```text
POST /api/admin/v1/providers/{connection_id}/test
POST /api/admin/v1/providers/{connection_id}/discover-models
POST /api/admin/v1/providers/{connection_id}/sync
```

Avoid provider-specific control-plane routes unless a provider exposes a truly unique capability.

## Acceptance criteria

A static code search of routing core must not reveal provider-specific branching.

Permitted locations for provider names:

- templates
- drivers
- plugins
- tests
- migration adapters
- documentation

---

# 6. P0-03 — Complete Generic Provider Drivers

## Problem

Generic drivers currently contain placeholder behavior such as:

```python
validate_connection() -> always OK
discover_models() -> []
execute() -> stub response
fetch_quota() -> []
```

This creates a false impression that dynamic provider onboarding is already implemented.

## Impact

The UI may report success for invalid URLs or invalid credentials.

The provider registry can pass shape-level acceptance tests without supporting real traffic.

## Required implementation

Implement complete drivers for:

### OpenAI-compatible

Minimum support:

```text
GET /models
POST /chat/completions
streaming SSE
Bearer auth
custom headers
custom base URL
timeout
usage parsing
rate-limit header parsing
error classification
```

Optional but desirable:

```text
POST /responses
embeddings later
```

### Anthropic-compatible

Minimum support:

```text
POST /v1/messages
streaming
x-api-key
anthropic-version
usage parsing
error classification
```

### Gemini-compatible

Minimum support:

```text
model discovery
generateContent
streamGenerateContent
usage_metadata parsing
quota/rate metadata when available
```

## Driver factory

Do not instantiate drivers inconsistently.

Use a common factory:

```python
driver = registry.create(
    driver_id=definition.driver,
    context=DriverContext(
        connection=connection,
        credential=credential,
        runtime=runtime_services,
    ),
)
```

Drivers should not require arbitrary constructor signatures.

## Acceptance criteria

A provider added through the Control Plane must:

```text
Add
↓
Test real connection
↓
Discover real models
↓
Import model
↓
Activate
↓
Receive a real routed request
```

without server restart.

---

# 7. P0-04 — Replace in-memory Control Plane state with PostgreSQL

## Problem

Control Plane state currently relies heavily on process-local dictionaries.

Examples include state for:

- provider connections
- credentials
- projects
- budgets
- alerts
- quota resources
- audit events
- settings

## Impact

Restarting the gateway can lose configuration.

Multiple gateway instances do not share a consistent state.

A provider configured in UI may not actually become part of routing runtime.

## Required fix

Create durable tables for at least:

```text
provider_definitions
provider_connections
provider_credentials
provider_models
model_resources
routes
route_candidates
routing_policies
projects
project_api_keys
project_budgets
config_revisions
audit_logs
quota_resources
```

The exact naming may differ, but the domain separation must remain.

## Credential model

A provider connection must support multiple credentials.

Do not store one encrypted credential directly on the connection record.

Use:

```text
provider_connection
   ├─ credential A
   ├─ credential B
   └─ credential C
```

Each credential must have its own:

```text
enabled
priority
weight
health
cooldown
quota state
last error
metadata
```

## Acceptance criteria

- create provider + credential;
- restart all gateway processes;
- provider still exists;
- credential remains usable;
- active config is restored;
- requests route through the provider after restart.

---

# 8. P0-05 — Make Config Revision real

## Problem

Config revision and validation currently contain mock behavior.

For example, validation may always return success and runtime snapshot generation may return empty/default snapshots.

## Impact

The user may believe config changes are atomic while runtime continues using stale or unrelated state.

## Required architecture

Implement:

```text
Draft Config Revision
        ↓
Schema Validation
        ↓
Semantic Validation
        ↓
Dependency Validation
        ↓
Runtime Compilation
        ↓
Persist Revision
        ↓
Atomic Activation
        ↓
Redis revision notification
        ↓
Gateway snapshot reload
```

## Semantic validation must catch

Examples:

- route references missing model resource;
- model resource references disabled credential;
- invalid provider driver;
- duplicate aliases;
- quota resource references unknown scope;
- route has no eligible candidates;
- invalid model capability constraints;
- invalid secret reference;
- unsupported protocol mapping.

## Runtime snapshot

The runtime snapshot should be immutable.

Example conceptual structure:

```python
RuntimeSnapshot(
    revision_id=...,
    provider_connections=...,
    credentials=...,
    resources=...,
    routes=...,
    policies=...,
    quota_bindings=...,
)
```

Request handling must read from the current snapshot without rebuilding the full configuration.

## Acceptance criteria

- invalid revision cannot activate;
- activation is atomic;
- all gateway instances converge on the same revision;
- restart loads active revision;
- rollback to previous revision works.

---

# 9. P0-06 — Fix SchedulableResource identity everywhere

## Problem

The resource abstraction includes credential scope, but several runtime components key state only by connection and model.

Example incorrect key:

```text
connection_id:model_id
```

Correct identity:

```text
connection_id:credential_id:model_id
```

## Impact

One credential being throttled can incorrectly disable another credential.

This breaks multi-account scheduling and quota isolation.

## Required fix

Introduce a single canonical resource key utility.

Example:

```python
@dataclass(frozen=True)
class ResourceKey:
    connection_id: str
    credential_id: str
    model_id: str

    def as_string(self) -> str:
        ...
```

Use this key in:

- circuit breaker
- health state
- latency tracker
- failure tracker
- session affinity
- quota binding
- usage ledger
- scheduler metrics
- request attempts

## Acceptance test

Configure:

```text
Connection X
 ├─ Credential A
 │   └─ Model M
 └─ Credential B
     └─ Model M
```

Make A return `401`.

Expected:

```text
A disabled
B remains healthy
B continues receiving requests
```

Any behavior that disables B is incorrect.

---

# 10. P0-07 — Complete the Universal Quota Engine

## Problem

The project has a promising quota abstraction and atomic reservation, but the full lifecycle is incomplete.

Known issues include:

- incomplete reset/window lifecycle;
- parent relationship not fully persistent;
- scheduler does not evaluate all quota dimensions;
- different units can be mixed incorrectly;
- burn-rate is not yet true burn-rate;
- Redis access pattern can become expensive.

---

## 10.1 QuotaResource schema

A quota resource should support at least:

```text
id
scope_type
scope_id
metric
window_type
window_seconds
limit
used
reserved
remaining
reset_at
window_start
window_end
replenish_rate
hard_limit
source
confidence
shared_group_id
parent_id
updated_at
```

Not every provider will populate every field.

---

## 10.2 Never mix native units

Do not compare:

```text
80 requests
200000 tokens
12 USD
```

using direct `min()` or `max()`.

Normalize each dimension first.

Example:

```text
RPM:
remaining_ratio = 0.80

TPM:
remaining_ratio = 0.40

daily:
remaining_ratio = 0.75
```

Then derive:

```text
effective_headroom = min(all remaining ratios)
```

or:

```text
pressure = max(all pressure ratios)
```

depending on policy.

---

## 10.3 Resource eligibility

A resource is schedulable only when **all applicable hard constraints** pass.

Example:

```text
ModelResource
   ├─ credential RPM
   ├─ credential TPM
   ├─ account daily quota
   ├─ provider weekly quota
   ├─ workspace budget
   └─ concurrency quota
```

Eligibility is:

```text
ALL(hard constraints available)
```

not a single quota lookup.

---

## 10.4 Persist quota hierarchy

If runtime supports:

```text
parent_id
shared_group_id
```

the database must also persist them.

Quota graph reconstruction after restart must produce the same hierarchy.

---

## 10.5 Implement real window reset semantics

The system must support quota reset behavior such as:

```text
minute
hour
day
week
month
provider-defined reset timestamp
rolling window
credit pool
```

Do not assume all quotas reset periodically.

---

## 10.6 Burn rate

Do not call:

```text
1 - remaining / limit
```

burn rate.

That is quota pressure.

Real burn-rate logic should consider:

```text
remaining
time until reset
recent consumption rate
forecast demand
```

Example:

```text
target_burn_rate =
    effective_remaining / time_until_reset
```

This is not required for the first quota correctness milestone, but naming must remain correct.

---

# 11. P0-08 — Fix Redis request-path behavior

## Problem

Current quota loading can involve:

```text
SCAN quota:*
```

followed by multiple:

```text
HGETALL
```

operations.

Doing this on the request path will not scale.

## Required architecture

Use a local immutable runtime quota index.

Concept:

```text
Redis
  ↓
quota updates / pub-sub / stream
  ↓
Runtime Quota Index
  ↓
request-time read
```

At scheduling time:

```text
request
  ↓
read local quota snapshot
  ↓
filter candidates
  ↓
score candidates
  ↓
choose candidate
  ↓
Redis Lua atomic reservation
```

Redis should remain authoritative for atomic reservation, but candidate evaluation should avoid scanning the entire quota store.

## Acceptance criteria

Routing complexity should scale primarily with:

```text
number of candidates in the selected route
```

not:

```text
number of all quota resources in the system
```

---

# 12. P0-09 — Make Usage Ledger always available without blocking request latency

## Problem

Usage Ledger may be disabled by default because database writes can occur directly on the request path.

This creates a bad tradeoff:

```text
ledger OFF
→ fast but blind

ledger ON
→ observable but slower
```

## Required fix

Introduce asynchronous usage event persistence.

Recommended flow:

```text
Request
  ↓
emit UsageEvent
  ↓
Redis Stream / async queue
  ↓
return request result

Worker
  ↓
batch events
  ↓
PostgreSQL bulk insert
```

Possible events:

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

## Requirements

The request path may synchronously record only the minimum state required for correctness.

Analytics must not require synchronous DB flushes.

## Acceptance criteria

- Usage Ledger is enabled by default.
- temporary PostgreSQL slowness does not directly stall every streaming response;
- events are retried or buffered;
- request/attempt/usage relationships remain reconstructable.

---

# 13. P0-10 — Replace full SSE buffering with incremental usage parsing

## Problem

Streaming code currently buffers the complete SSE stream to parse usage after completion.

## Impact

Memory usage grows with:

```text
response_size × concurrent_streams
```

For large coding sessions this can become significant.

## Required design

Use an incremental `UsageTap`.

```text
upstream SSE chunk
      │
      ├────────────→ client immediately
      │
      └────────────→ incremental SSE parser
                            ↓
                       usage metadata
```

The parser should hold only:

- incomplete SSE frame;
- small protocol state;
- usage counters;
- final usage metadata.

It should not hold the complete generated response.

## Invariant

The first-token failover rule must remain unchanged:

> After any response token/event has been forwarded to the client, the router must not switch to another upstream model.

---

# 14. P0-11 — Remove synchronous thread joins from async request paths

## Problem

Utilities that call:

```text
thread.start()
thread.join(...)
```

inside async request processing can block the event loop.

## Required fix

Maintain async all the way through the request path.

Preferred:

```python
await async_function()
```

Avoid:

```text
async
→ synchronous wrapper
→ create thread
→ create event loop
→ join
```

If a blocking library is unavoidable:

```python
await asyncio.to_thread(...)
```

must be used carefully and outside critical routing loops.

---

# 15. P0-12 — Separate capability and quality signals

## Problem

Capability and quality are conceptually different but are currently partially conflated.

Capability asks:

```text
Can this resource perform the task?
```

Examples:

- tools
- vision
- streaming
- context size
- structured output

Quality asks:

```text
How well is this model expected to perform the task?
```

## Required design

Use:

```text
capability_fit
```

as eligibility or hard/soft constraints.

Use a separate:

```text
quality_score
```

for scheduling.

Example:

```text
Model A:
tools = true
coding_quality = 0.94

Model B:
tools = true
coding_quality = 0.71
```

Both pass capability filtering, but quality score differs.

---

# 16. P0-13 — Formalize failure scope

## Problem

Error classification is already reasonably strong, but error scope must become a first-class type.

## Required model

Example:

```python
class FailureScope(Enum):
    REQUEST = "request"
    MODEL = "model"
    CREDENTIAL = "credential"
    CONNECTION = "connection"
    PROVIDER = "provider"
    QUOTA_RESOURCE = "quota_resource"
```

And:

```python
FailureDecision(
    kind=...,
    scope=...,
    scope_id=...,
    retryable=...,
    cooldown_until=...,
    disable=...,
)
```

## Examples

### Invalid request

```text
kind = INVALID_REQUEST
scope = REQUEST
retryable = false
```

Do not penalize credentials.

### Invalid API key

```text
kind = AUTH_REVOKED
scope = CREDENTIAL
```

Disable only that credential.

### Model overloaded

```text
kind = MODEL_OVERLOADED
scope = MODEL
```

Other models on the provider may remain usable.

### TPM exceeded

```text
kind = RATE_LIMIT
scope = QUOTA_RESOURCE
```

Cooldown only the affected quota/resource.

---

# 17. P0-14 — Make distributed state truly distributed

## Problem

Some runtime components still live only in process memory.

Examples may include:

- circuit breaker
- session affinity
- failure tracker
- latency tracker
- config state

## Required split

### Local-only cache is acceptable for

```text
compiled routing tables
capability index
read-only snapshot
recent metrics cache
```

### Redis-backed state is required for

```text
cooldowns
distributed circuit state
quota reservations
concurrency
session binding when cross-instance consistency matters
revision notification
```

## Acceptance test

Run two gateway instances.

Trigger a credential cooldown through Gateway A.

Gateway B must stop selecting the credential within the expected propagation window.

---

# 18. P0-15 — Implement worker responsibilities

## Problem

The worker currently performs very limited responsibilities and does not yet match the target architecture.

## Worker responsibilities

The worker layer should eventually own:

```text
provider catalog sync
quota sync
health probes
usage persistence
usage aggregation
pricing snapshots
quota reconciliation
stale reservation cleanup
metric aggregation
alert evaluation
forecast input preparation
```

Not all need to be finished at once, but P0 requires enough worker functionality to remove expensive persistence work from the request path.

## Minimum P0 worker scope

Implement at least:

```text
Usage event persistence
Quota synchronization
Stale reservation cleanup
Provider catalog sync framework
```

---

# 19. P0-16 — Harden Control Plane provider onboarding

The provider workflow must become real.

Required workflow:

```text
Add Provider
     ↓
Select Driver
     ↓
Configure Base URL
     ↓
Add Credential
     ↓
Test Connection
     ↓
Discover Models
     ↓
Import Models
     ↓
Configure Capabilities
     ↓
Create / Assign Route
     ↓
Validate Revision
     ↓
Activate
```

The system must not require restart.

The UI must not hardcode provider identities.

---

# 20. P0-17 — Fix health/readiness semantics

## Required endpoints

### Liveness

```text
/health/live
```

Checks only whether the process is alive.

### Readiness

```text
/health/ready
```

Must actively check:

- PostgreSQL connectivity;
- Redis connectivity;
- active runtime snapshot;
- required encryption secret;
- required runtime services.

Do not report a dependency as connected merely because a client object was constructed.

---

# 21. P0-18 — Production secret handling

## Encryption key

In production:

```text
SMART_ROUTER_ENCRYPTION_KEY
```

must be mandatory.

The application must fail startup if a production environment does not provide it.

A predictable development fallback may be allowed only in explicit development mode.

## Credential display

Never return plaintext credentials.

UI may show only safe metadata such as:

```text
key suffix
credential name
created time
last used
status
```

---

# 22. P0-19 — Add SSRF protection to provider configuration

Because users can enter arbitrary provider URLs, outbound provider calls can become an SSRF vector.

## Default policy

Block:

```text
localhost
127.0.0.0/8
::1
169.254.169.254
link-local networks
private network ranges
internal service hostnames
unsupported schemes
```

unless explicitly allowed.

## Private provider support

Allow explicit opt-in:

```text
allow_private_network = true
```

for trusted local providers such as:

```text
Ollama
vLLM
LAN gateways
CLIProxy local
```

Redirects must be revalidated.

---

# 23. P0-20 — Fix dependency and CI reproducibility

## Current issue

Tests import `respx`, but development dependencies do not fully declare it.

## Required dev dependencies

Ensure all test-only packages are declared explicitly.

At minimum verify:

```text
pytest
pytest-asyncio
fakeredis
respx
```

plus any additional test packages actually imported by the suite.

## Add CI

Minimum CI stages:

```text
lint
type/schema checks
unit tests
integration tests
migration tests
Docker build
acceptance smoke tests
```

CI must run from a clean checkout.

No locally installed package may be required implicitly.

---

# 24. Mandatory end-to-end tests

These tests are blockers.

---

## E2E-01 — Dynamic provider onboarding

Start with an empty runtime.

Perform:

```text
Create provider connection
↓
Create credential
↓
Test real mock endpoint
↓
Discover models
↓
Import model
↓
Create route
↓
Activate config revision
↓
POST /v1/chat/completions
```

Verify:

```text
mock upstream receives request
response succeeds
request ledger exists
attempt ledger exists
usage event exists
quota reservation reconciles
```

Then restart Smart Router and run the request again.

It must still succeed.

---

## E2E-02 — Credential isolation

Create:

```text
Provider X
 ├─ Credential A
 │   └─ Model M
 └─ Credential B
     └─ Model M
```

Make Credential A return `401`.

Verify:

```text
A becomes unavailable
B remains available
B receives subsequent requests
```

---

## E2E-03 — Multi-dimensional quota

Configure one resource with:

```text
RPM: 90% remaining
TPM: 5% remaining
Daily: 80% remaining
```

Verify scheduler sees the effective pressure as dominated by TPM.

Do not compare raw units.

---

## E2E-04 — Concurrent atomic reservation

Given:

```text
remaining tokens = 100000
```

submit concurrently:

```text
R1 reserve 40000
R2 reserve 40000
R3 reserve 35000
```

Expected:

```text
R1 succeeds
R2 succeeds
R3 cannot reserve this resource
```

No oversubscription is permitted.

---

## E2E-05 — Cross-instance cooldown

Run Gateway A and Gateway B.

Credential A is throttled through Gateway A.

Gateway B must observe the cooldown and stop scheduling A.

---

## E2E-06 — Streaming memory

Stream a large response from a mock provider.

Verify memory usage does not grow approximately linearly with the entire response body for every concurrent stream.

The router must use incremental parsing.

---

## E2E-07 — Config rollback

Activate revision N.

Attempt invalid revision N+1.

N+1 must fail validation.

Runtime remains on N.

Activate valid N+2.

Rollback to N.

All gateway instances converge correctly.

---

# 25. Performance requirements

Do not optimize arithmetic prematurely.

The current scoring computations are already fast enough.

The main optimization targets are I/O architecture.

## Required direction

Avoid:

```text
Redis SCAN on every request
N Redis HGETALL per request
synchronous PostgreSQL flushes per event
full SSE buffering
blocking thread joins
duplicate router paths
```

Prefer:

```text
immutable runtime snapshots
local read indexes
atomic Redis mutation only when needed
async event persistence
batch writes
incremental streaming parsers
```

---

# 26. Performance budget

The router overhead should remain small relative to upstream LLM latency.

Target after warmup:

```text
candidate filtering + scoring:
p95 < 2 ms

local runtime snapshot read:
sub-millisecond

quota reservation Redis call:
single network round trip where possible

request path DB writes:
0 synchronous analytics writes

stream forwarding:
constant-memory incremental processing
```

These values are targets, not hard protocol guarantees, but regressions must be measured.

---

# 27. What must NOT be implemented during P0

Until this document is complete, do not prioritize:

```text
new provider count
plugin marketplace
ML forecasting
advanced semantic routing
multi-region deployment
SSO
billing SaaS
image/video expansion
fusion/judge routing
complex UI redesign
advanced quota prediction
shadow-price ML
```

These are useful later but currently increase architectural divergence.

---

# 28. What may continue during P0

Allowed supporting work:

```text
tests
database migrations
driver implementation
Control Plane persistence
runtime snapshot compiler
quota correctness
usage event pipeline
Redis state synchronization
streaming parser improvements
security hardening
observability required for validation
```

---

# 29. Required implementation order

AI agents must execute P0 in approximately this dependency order.

## Stage A — Runtime convergence

```text
P0-01 Single RouterEngine
P0-02 Provider-independent core
P0-06 Canonical SchedulableResource identity
```

## Stage B — Provider system reality

```text
P0-03 Real Generic Drivers
P0-04 PostgreSQL Control Plane
P0-05 Real Config Revision
P0-16 Real provider onboarding
```

## Stage C — Resource correctness

```text
P0-07 Universal Quota Engine
P0-08 Redis runtime index
P0-13 Failure scope
P0-14 Distributed runtime state
```

## Stage D — Telemetry and streaming

```text
P0-09 Async Usage Ledger
P0-10 Incremental SSE UsageTap
P0-11 Async-only request path
P0-15 Worker baseline
```

## Stage E — Hardening

```text
P0-17 health/readiness
P0-18 secrets
P0-19 SSRF
P0-20 CI/dependencies
```

## Stage F — Acceptance

Run all mandatory E2E tests.

Only then continue `README.md`.

---

# 30. Definition of P0 complete

P0 is complete only when all conditions below are true.

- [ ] One authoritative RouterEngine handles all request protocols.
- [ ] Router core contains no provider-specific branching.
- [ ] Generic OpenAI provider can be added and used without code changes.
- [ ] Generic Anthropic provider can be added and used without code changes.
- [ ] Provider configuration persists across restart.
- [ ] Multiple credentials belong to one connection cleanly.
- [ ] SchedulableResource identity includes credential.
- [ ] Config revision validation is real.
- [ ] Runtime activation is atomic.
- [ ] Invalid config cannot replace last-known-good config.
- [ ] PostgreSQL is durable Control Plane truth.
- [ ] Redis provides distributed runtime coordination.
- [ ] Quota hierarchy persists and reconstructs.
- [ ] Scheduler evaluates all relevant quota dimensions.
- [ ] Native quota units are never directly mixed.
- [ ] Atomic quota reservation passes concurrency tests.
- [ ] Usage Ledger is enabled without synchronous analytics writes.
- [ ] Streaming usage parsing is incremental.
- [ ] Credential-scoped failures do not poison unrelated credentials.
- [ ] Cross-instance cooldown works.
- [ ] Readiness actively checks dependencies.
- [ ] Production encryption key is mandatory.
- [ ] Provider URLs are protected against SSRF.
- [ ] Clean checkout CI passes.
- [ ] Dynamic provider onboarding E2E passes.
- [ ] Restart persistence E2E passes.
- [ ] Config rollback E2E passes.

---

# 31. AI coding agent instructions

This section is mandatory for AI agents working on the repository.

## Before editing code

Always determine:

1. Which P0 issue is being solved?
2. Which architectural invariant does it affect?
3. Is the proposed code adding another parallel implementation?
4. Can the solution reuse an existing abstraction?
5. What integration test will prove the feature is real?

Do not add code only to make an isolated unit test green.

---

## Never solve architecture problems with new provider conditionals

Forbidden pattern:

```python
if provider == "aibox":
    ...
elif provider == "groq":
    ...
elif provider == "gemini":
    ...
```

unless the code is inside a provider-specific driver/template/plugin.

---

## Do not create another source of truth

Before introducing state, classify it:

```text
durable domain state
    → PostgreSQL

distributed transient state
    → Redis

immutable/read cache
    → process memory
```

Do not create a fourth source of truth.

---

## Preserve backward compatibility through adapters

Legacy configuration may continue to work during migration.

But legacy configuration must be converted into the new runtime domain.

Do not keep the old runtime alive merely to preserve old config.

---

## Every P0 change requires tests

At minimum:

```text
unit test
integration test
```

For lifecycle changes:

```text
restart / persistence test
```

For distributed state:

```text
multi-instance test
```

For concurrency:

```text
parallel reservation test
```

---

## Prefer correctness before optimization

First guarantee:

```text
correct resource identity
correct quota semantics
correct failure scope
correct persistence
```

Then optimize Redis and DB access.

Do not introduce performance shortcuts that invalidate resource correctness.

---

# 32. Recommended first implementation tasks

The first concrete PR sequence should be:

## PR-01 — Canonical ResourceKey

Introduce:

```text
connection_id
credential_id
model_id
```

and migrate circuit/failure/latency/session/usage keys.

Add credential isolation tests.

---

## PR-02 — Runtime Snapshot model

Define the immutable runtime snapshot and compiler interface.

Do not yet remove legacy config.

---

## PR-03 — LegacyConfigAdapter

Compile the existing YAML configuration into the new runtime snapshot.

All existing tests should continue passing through the new RouterEngine.

---

## PR-04 — Make RouterEngine authoritative

Remove production dual-path routing.

All `/v1/messages` and `/v1/chat/completions` requests use the new engine.

---

## PR-05 — Persistent Provider Registry

Move provider connections/credentials/models from in-memory admin dictionaries into PostgreSQL.

---

## PR-06 — Real OpenAI-compatible driver

Implement:

```text
test
discover
execute
stream
usage
errors
rate-limit metadata
```

Then implement E2E-01.

---

## PR-07 — Config revision compiler + activation

Real validation, activation, last-known-good snapshot, rollback.

---

## PR-08 — Quota schema correctness

Persist:

```text
parent_id
shared_group
reset_at
window metadata
source
confidence
```

Normalize quota pressure per dimension.

---

## PR-09 — Runtime quota index

Remove full Redis scans from request routing.

Use Redis only for authoritative mutations/reservations and update propagation.

---

## PR-10 — Async usage pipeline + UsageTap

Remove synchronous analytics writes and full SSE buffering.

---

# 33. Exit gate before returning to README roadmap

The implementation may continue with the main `README.md` roadmap only when:

```text
P0 Definition of Complete == true
AND
mandatory E2E tests == passing
AND
clean checkout CI == passing
```

At that point the project can safely proceed to:

```text
advanced scheduler optimization
burn-rate planning
scarcity
expected retry cost
forecasting
token optimization plane
additional provider templates
advanced Control Plane UX
```

Until then, implementation effort should remain focused on architectural convergence and runtime correctness.

---

# 34. Final architectural target after P0

After stabilization, the project should behave as one coherent system:

```text
                         CLIENT
                           │
                           ▼
                Universal API Gateway
                  OpenAI / Anthropic
                           │
                           ▼
                 Canonical Request
                           │
                           ▼
                 Capability Resolver
                           │
                           ▼
                 Resource Resolver
                           │
                           ▼
                   Quota Graph
                           │
                           ▼
                   Smart Scheduler
                           │
                  atomic reservation
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
              ┌────────────┴────────────┐
              ▼                         ▼
           Redis                     Worker
       runtime state                    │
                                        ▼
                                   PostgreSQL
                                  durable truth
```

This architecture, not feature count, is the stabilization objective.

