# Smart Router

> **Implementation baseline for AI coding agents and human contributors**
> Version: **1.0**
> Status: **Architecture and implementation baseline**
> Scope: **Gateway + Provider Registry + Usage Ledger + Universal Quota Engine + Smart Scheduler + Control Plane**

Smart Router is a self-hosted, provider-agnostic AI resource orchestrator. It evolves the current `smart-router-ui` codebase from a hardcoded multi-upstream failover proxy into a system where users can add and manage AI providers, credentials, models, quotas, budgets, and routing policies without changing the routing core.

The main product goal is not merely to support many AI providers. The main goal is to treat all AI capacity available to the user as a unified resource pool and allocate requests to the most appropriate resource while minimizing paid fallback and avoiding waste of legitimate free capacity.

This README is the **primary implementation contract** for the 1.0 project. It is intentionally written so that an AI coding model can enter the repository, understand the intended architecture, and implement the system incrementally without relying on prior conversation history.

---

## 1. AI Implementation Contract

### 1.1 Read this before modifying code

When implementing Smart Router, follow this priority order:

1. **Architecture Decisions (ADR)** in this README.
2. **1.0 scope and invariants**.
3. **Domain model and interfaces**.
4. **Milestone dependency order**.
5. **Acceptance criteria and tests**.
6. Examples and suggested defaults.

If two sections appear to conflict, prefer the item higher in the list above.

### 1.2 Non-negotiable rules

An AI coding agent or human contributor **MUST NOT**:

- add provider-name conditionals such as `if provider == "aibox"` inside `RouterEngine`, scheduling, quota, or generic gateway core;
- add a new upstream by editing hardcoded routing branches;
- treat quota as a single `remaining_tokens` field;
- silently treat estimated token/quota values as exact provider truth;
- route a stream to another model after the first upstream output has already been sent to the client;
- store provider secrets in plaintext database fields, logs, audit diffs, or API responses;
- make ProxyPal a runtime dependency;
- route all providers through CLIProxyAPI when a direct generic provider driver is sufficient;
- implement automatic account creation, account rotation, or other mechanisms intended to bypass provider restrictions or rate limits;
- introduce arbitrary executable Python/JavaScript/shell code in provider manifests;
- start ML-based forecasting before the Usage Ledger and Quota Engine have reliable telemetry;
- remove compatibility with existing logical routes during migration unless an explicit migration phase says so.

### 1.3 Required implementation behavior

Every significant code change should preserve these principles:

- provider-specific behavior belongs in a **driver, template, collector, bridge, or legacy compatibility module**;
- PostgreSQL is the durable source of truth;
- Redis coordinates short-lived distributed runtime state;
- configuration changes are versioned and activated atomically;
- request routing is deterministic and explainable in 1.0;
- estimates carry `source` and `confidence` metadata;
- quota admission is checked before upstream execution when enough information exists;
- retry cost and possible repeated token consumption are part of routing economics;
- paid fallback is policy-controlled, never implicitly mixed into the free primary pool;
- current behavior is migrated incrementally rather than replaced in a big-bang rewrite.

### 1.4 How an AI agent should work on this repository

Before implementing a task:

1. Identify the milestone and acceptance criteria affected.
2. Inspect current source and existing tests.
3. Determine whether the change belongs to core, driver, resource plane, control plane, or compatibility code.
4. After analysis and planning, ask model `claude-router-review` to review whether the plan is sufficiently optimized, scoped, and architecture-consistent before coding.
5. Add or update tests for the invariant being changed.
6. Prefer the smallest architecture-consistent change over unrelated refactoring.
7. If database schema changes, add an Alembic migration.
8. If an API contract changes, update the relevant schema and contract tests.
9. If a new provider is added, implement it through a generic template/driver or a provider-specific driver outside routing core.
10. After each coding step, ask model `claude-router-review` to review whether the code is correct, simple, secure, and aligned with this README before continuing.
11. Run unit, contract, and relevant integration tests before considering the task complete.
12. Update this README only when architecture, contracts, setup, or scope changes.

If a requirement is ambiguous, choose the option that preserves provider agnosticism, truthful accounting, backward compatibility, and the milestone dependency order.

---

## 2. Product Definition

Smart Router exposes a single AI gateway to clients such as Claude Code, OpenAI-compatible SDKs, coding agents, internal applications, and automation tools.

Users connect multiple resource sources, for example:

- OpenAI-compatible API endpoints;
- Anthropic-compatible endpoints;
- Gemini-compatible endpoints;
- local model gateways;
- multiple API keys for the same connection;
- CLIProxyAPI-managed OAuth or subscription pools;
- custom provider endpoints described by a safe declarative manifest.

Smart Router then decides which physical resource should serve each logical request.

A physical resource is not simply a model name. It is the schedulable combination of connection, credential scope, and model plus all constraints that apply to it.

### 2.1 Product objective

The scheduler should minimize:

- paid fallback cost;
- unused free capacity that is about to expire/reset;
- retry-induced token waste;
- failures and throttling;
- unnecessary cache loss;
- latency when required by policy.

Subject to:

- required model capabilities;
- minimum quality level;
- context/output requirements;
- reliability targets;
- project budgets;
- provider quota and rate limits;
- administrator policy.

### 2.2 What Smart Router is not

Smart Router is not intended to be:

- a quota bypassing tool;
- an automatic account creator;
- a generic arbitrary-code plugin host in 1.0;
- a full SaaS billing platform in 1.0;
- an ML optimizer before sufficient telemetry exists;
- a replacement for CLIProxyAPI's OAuth lifecycle in 1.0.

---

## 3. Current Baseline

The existing `smart-router-ui` source is the migration baseline and should be evolved rather than discarded.

Current code includes:

- FastAPI;
- async `httpx` / HTTP2 upstream calls;
- Anthropic-compatible endpoints;
- logical routes/models;
- priority routing;
- smooth weighted round-robin;
- primary/fallback separation;
- circuit cooldown;
- streaming;
- AI-BOX-specific model catalog synchronization;
- last-known-good catalog state;
- provider-specific configuration in `config.yaml`;
- current router/catalog/state tests.

Important current files include:

```text
router.py
config.yaml
aibox_catalog.py
tests/test_router.py
tests/test_catalog.py
tests/test_state.py
```

### 3.1 Problems in the current implementation

| Current design | Useful property | Limitation to remove |
|---|---|---|
| `Candidate(upstream, model, weight)` | Simple routing representation | Conflates connection, credential and model; insufficient for resource-level scheduling |
| YAML `upstreams` | Easy local setup | Requires manual edits/restart and provider-specific auth/config |
| In-memory circuit state | Fast | Lost on restart and not shared across gateway instances |
| AI-BOX catalog module | Runtime verification and last-known-good behavior | Provider-specific discovery/promotion is coupled to one provider |
| Priority + weighted routing | Predictable | Ignores quota, credentials, latency, cache, retry cost and budgets |
| File logging | Basic visibility | No durable request/attempt/usage ledger |
| Reactive `429` handling | Provides fallback | Acts after quota/rate limit has already been hit |

### 3.2 Behavior that must be preserved during migration

- Keep FastAPI/Python for gateway 1.0.
- Keep logical route names so clients do not need physical provider/model knowledge.
- Preserve existing Anthropic-compatible routes.
- Preserve primary versus fallback semantics.
- Preserve last-known-good configuration/catalog behavior.
- Preserve the streaming invariant: transparent failover only before the first output reaches the client.
- Keep a compatibility path for legacy `config.yaml` until migration is complete.

---

## 4. Scope for Version 1.0

### 4.1 MUST

Version 1.0 is not complete unless all items below are implemented:

- Add, update, disable, test and remove provider connections without modifying `RouterEngine`.
- Add multiple credentials to a provider connection.
- Implement generic OpenAI-compatible, Anthropic-compatible and Gemini-compatible drivers.
- Support model discovery when the upstream exposes a suitable endpoint and manual model entry otherwise.
- Integrate CLIProxyAPI in **managed-pool mode**.
- Track request and upstream attempts.
- Track input/output/total/cached/reasoning token fields when available.
- Track latency, status, error kind, failover reason and actual/estimated cost.
- Implement multidimensional `QuotaResource` constraints.
- Implement atomic quota reservation with Redis.
- Reject ineligible resources before upstream execution when capability, health, quota or budget makes them invalid.
- Implement a configurable deterministic scheduler.
- Provide PostgreSQL-backed configuration, usage ledger and audit history.
- Provide Redis-backed reservation, circuit, concurrency and session state.
- Version configuration and support atomic hot activation.
- Provide a Control Plane with at least Overview, Providers, Models/Resources, Routes/Policies, Usage/Requests and Settings/Security.
- Preserve Anthropic compatibility and provide a migration path from legacy `config.yaml`.
- Ship a reproducible Docker Compose deployment.

### 4.2 SHOULD

High-priority items that should be included if they do not threaten 1.0 stability:

- OpenAI `/v1/chat/completions` compatibility;
- OpenAI `/v1/responses` compatibility covered by an explicit test matrix;
- session affinity and cache-locality scoring;
- project API keys, per-project budgets and concurrency limits;
- quota/provider degradation alerts;
- CLIProxy configuration import for compatible direct providers.

### 4.3 LATER

The following must not become hidden dependencies of 1.0:

- CLIProxy transparent account-level scheduling;
- public plugin marketplace;
- arbitrary provider scripting;
- native reimplementation of all CLI subscription OAuth flows;
- complex ML demand forecasting;
- enterprise SSO and full SaaS billing;
- multi-region scheduling;
- remote unsigned provider-template registry.

---

## 5. Core Architecture

Smart Router is divided into four conceptual planes.

```text
CLIENTS
Claude Code / OpenAI SDK / Agents / Applications
                     |
                     v
+--------------------------------------------------+
|                  CONTROL PLANE                   |
| Providers / Credentials / Routes / Policies      |
| Projects / API Keys / Revisions / Audit          |
+--------------------------------------------------+
                     |
               immutable config
                     |
                     v
+--------------------------------------------------+
|                   DATA PLANE                     |
| Gateway -> Protocol Adapter -> RouterEngine       |
| -> ProviderDriver -> Upstream                    |
+--------------------------------------------------+
                     |
                     v
+--------------------------------------------------+
|                 RESOURCE PLANE                   |
| Quota / Reservation / Health / Circuit           |
| Concurrency / Session binding / Cost             |
+--------------------------------------------------+
                     |
                     v
+--------------------------------------------------+
|               OPTIMIZATION PLANE                 |
| Eligibility / Scoring / Burn-rate / Scarcity     |
| Retry economics / Free capacity utilization      |
+--------------------------------------------------+
```

Durable and runtime state:

```text
PostgreSQL
  -> configuration
  -> config revisions
  -> audit
  -> request / attempt / usage ledger
  -> historical aggregates

Redis
  -> atomic quota reservations
  -> concurrency counters
  -> distributed circuit state
  -> rate-limit state
  -> session bindings
  -> short-lived runtime coordination

Worker
  -> model discovery
  -> quota synchronization
  -> health probes
  -> usage aggregation
  -> retention cleanup
```

### 5.1 Runtime component responsibilities

| Component | Must do | Must not do |
|---|---|---|
| Gateway API | Authentication, protocol parsing, streaming, client response | Hardcode provider policy |
| RouterEngine | Resolve route, filter candidates, plan, score, retry | Know concrete provider URLs or keys |
| ResourceRegistry | Expose immutable runtime resources/policies | Perform arbitrary network calls in routing hot path |
| QuotaEngine | Check, reserve, reconcile quota constraints | Present estimates as exact truth |
| ProviderDriver | Translate, authenticate, execute, parse usage/errors | Decide global routing policy |
| Worker | Sync model/quota/health and aggregate telemetry | Be the only holder of durable state |
| Control Plane | CRUD, validation, revisions, policy simulation | Mutate live runtime objects bypassing revision activation |

---

## 6. Request Lifecycle

Every request should follow this order.

1. **Authenticate client** and resolve workspace/project/API-key policies.
2. **Create `RoutingEnvelope`** containing only routing metadata while retaining the original request payload.
3. **Resolve logical route** to candidate `ModelResource` objects.
4. **Capability filter** candidates.
5. **Resource planner** checks auth state, health, circuit, quota graph, concurrency, project budget and paid-fallback policy.
6. **Estimate consumption** for each candidate.
7. **Score eligible resources** according to the selected routing policy.
8. **Atomically reserve** quota/concurrency for the chosen resource.
9. **Execute through `ProviderDriver`**.
10. If execution fails before first output and the error is retryable, record the attempt and try the next resource within retry budgets.
11. **Reconcile** reserved versus actual usage.
12. Update quota/rate-limit observations and health state.
13. Persist request, attempt and usage events.
14. Return the provider response in the source protocol.

### 6.1 RoutingEnvelope

Do not canonicalize the full provider payload unless required. Protocol semantics differ and aggressive translation can lose fields.

Suggested structure:

```python
@dataclass
class RoutingEnvelope:
    request_id: str
    source_protocol: str
    logical_route: str
    required_capabilities: set[str]
    estimated_input_tokens: int | None
    max_output_tokens: int | None
    stream: bool
    session_hint: str | None
    project_id: str
    policy_overrides: dict
    raw_payload: object  # not persisted by default
```

### 6.2 Streaming invariant

**Never stitch responses from different models into one already-started stream.**

Transparent failover is allowed only while no upstream output has been sent to the client.

Once first output has been emitted:

- keep the same upstream stream;
- record any stream failure;
- return/terminate according to the source protocol;
- do not silently continue from another model.

---

## 7. Standard Domain Model

The standard resource model is foundational. Do not collapse these entities back into one `Provider` object.

```text
Workspace
  -> Project
      -> RouterApiKey

ProviderDefinition
  -> ProviderConnection
      -> Credential
      -> ModelDefinition
          -> ModelResource

ModelResource
  -> QuotaResource(s)
  -> SharedQuotaGroup(s)
  -> Health / Circuit / Concurrency state

LogicalRoute
  -> RoutingPolicy
      -> selectors / constraints / weights
```

### 7.1 ProviderDefinition

Describes **how a class of provider behaves**, not a concrete account.

Example responsibilities:

- driver identifier;
- protocol;
- supported auth schemes;
- discovery behavior;
- usage parsing capability;
- quota collection capability;
- error classification rules;
- built-in manifest/template version.

### 7.2 ProviderConnection

A concrete endpoint added by a user.

Examples:

- `Groq Personal`;
- `OpenRouter Main`;
- `Internal AI Gateway`;
- `Local Ollama Gateway`;
- `Local CLIProxy`.

Typical fields:

```text
id
provider_definition_id
name
base_url
proxy configuration
TLS policy
timeouts
enabled/state
metadata
config revision
```

### 7.3 Credential

A connection may contain one or more credentials.

Credential types may include:

- Bearer API key;
- `x-api-key`;
- custom header;
- query parameter token;
- OAuth identity managed by a bridge;
- service token;
- environment secret reference during legacy migration.

Each credential may have independent:

- health;
- auth state;
- quota;
- concurrency;
- priority/weight hint;
- last error;
- cooldown/reset state.

### 7.4 ModelDefinition

Represents model metadata exposed by a connection.

It should contain:

- provider model ID;
- display name;
- context window;
- max output;
- capabilities;
- pricing metadata;
- metadata source/confidence;
- lifecycle state.

### 7.5 ModelResource

The **schedulable physical resource**.

Conceptually:

```text
ProviderConnection + Credential selection scope + ModelDefinition
```

A model name alone is not enough to schedule correctly because two credentials may have different quotas, state, rate limits or subscription capacity.

### 7.6 LogicalRoute

A stable name exposed to clients, for example:

```text
auto
auto-free
fast
coding
review
critical
legacy logical route names
```

A logical route points to a `RoutingPolicy`, not directly to one fixed upstream model.

---

## 8. Provider Registry and Onboarding

Provider onboarding is a first-class product feature.

### 8.1 Add Provider flow

The UI/API should support this lifecycle:

1. Select provider type/template.
2. Enter connection endpoint and networking settings.
3. Configure authentication.
4. Test connection without logging secrets.
5. Discover models if supported.
6. Allow manual model configuration when discovery is unavailable.
7. Probe or override capabilities/pricing/quota metadata.
8. Validate configuration.
9. Publish a config revision.
10. Gateway instances atomically activate the new runtime snapshot.

### 8.2 Provider lifecycle

Suggested states:

```text
ProviderConnection:
  draft -> testing -> active -> degraded -> disabled

Credential:
  active -> rate_limited
         -> quota_exhausted
         -> auth_expired
         -> revoked
         -> disabled

ModelDefinition:
  discovered -> verified -> deprecated / hidden / disabled
```

Entities with usage history should normally be soft-deleted/deactivated rather than physically deleted.

---

## 9. Generic Provider Drivers

The goal is to support the majority of providers without adding provider-specific routing code.

### 9.1 Required generic drivers

Version 1.0 should provide:

- `openai-compatible`;
- `anthropic-compatible`;
- `gemini-compatible`;
- `cliproxy-bridge`.

Provider templates may preconfigure these drivers for known services, but templates do not change routing architecture.

### 9.2 Driver interface

The exact Python types may evolve, but the behavior boundary should remain similar to:

```python
class ProviderDriver(Protocol):
    async def validate_connection(self, ctx) -> DiagnosticResult: ...
    async def discover_models(self, ctx) -> list[DiscoveredModel]: ...
    async def execute(self, ctx, request) -> ProviderResponse: ...
    async def execute_stream(self, ctx, request) -> AsyncIterator[ProviderEvent]: ...
    async def fetch_quota(self, ctx) -> list[QuotaObservation]: ...

    def parse_usage(self, response) -> UsageObservation: ...
    def classify_error(self, error_or_response) -> ClassifiedError: ...
    def capabilities(self) -> DriverCapabilities: ...
```

`fetch_quota()` may legitimately return an empty list.

When provider quota APIs are unavailable, the Quota Engine can still use:

- response usage;
- rate-limit headers;
- configured internal caps;
- local counters;
- `429`/reset inference;
- conservative estimates.

### 9.3 No provider branches in core

This is forbidden:

```python
if provider_name == "aibox":
    ...
elif provider_name == "xkiro":
    ...
```

This is the desired architecture:

```python
driver = driver_registry.resolve(resource.driver_id)
response = await driver.execute(context, request)
```

Provider-specific behavior may exist in:

```text
providers/
templates/providers/
collectors/
compatibility/
```

but not in global routing decisions.

---

## 10. Declarative Provider Manifest

A provider manifest describes safe, declarative behavior for compatible endpoints.

Example:

```yaml
id: custom-openai
driver: openai-compatible

transport:
  base_url: https://example.invalid/v1

auth:
  type: bearer

models:
  discovery:
    method: GET
    path: /models

usage:
  input_tokens: usage.prompt_tokens
  output_tokens: usage.completion_tokens
  total_tokens: usage.total_tokens

rate_limits:
  remaining_requests: x-ratelimit-remaining-requests
  remaining_tokens: x-ratelimit-remaining-tokens
  reset_requests: x-ratelimit-reset-requests
  reset_tokens: x-ratelimit-reset-tokens

errors:
  - status: 429
    kind: RATE_LIMIT
    action: cooldown
```

### 10.1 Manifest safety boundary

Allowed:

- field mapping;
- JSON path extraction;
- header mapping;
- static whitelisted transformations;
- constrained regex classification;
- declarative auth metadata;
- declarative error rules.

Not allowed in 1.0:

- arbitrary Python;
- arbitrary JavaScript;
- shell commands;
- arbitrary executable hooks;
- unrestricted network calls defined inside manifests.

Complex provider logic belongs in a trusted driver. An out-of-process plugin SDK is post-1.0 work.

### 10.2 Provider Template Registry

Templates are convenience metadata for known providers.

A template can prefill:

- driver ID;
- base URL;
- auth scheme;
- model discovery path;
- usage fields;
- rate-limit headers;
- common error rules;
- known capability metadata.

Built-in templates are versioned in source. User-created templates may be stored in PostgreSQL.

Version 1.0 must not automatically download and execute code from a remote template registry.

---

## 11. Capability Model

Capability metadata should carry both a value and provenance.

Examples:

| Capability | Example value | Routing effect |
|---|---|---|
| `context_window` | `128000`, `200000`, `unknown` | Reject requests that exceed safe context |
| `max_output` | `8192`, `16384`, `unknown` | Validate output requirement |
| `tools` | supported / unsupported / unknown | Required for tool calls |
| `vision` | supported / unsupported / unknown | Required for image input |
| `structured_output` | schema / json-object / unknown | Match response format |
| `reasoning` | native / configurable / unknown | Policy signal |
| `streaming` | yes/no | Required by stream requests |

Each capability should ideally record:

```text
value
source = provider | template | probe | admin | inferred
confidence = exact | high | medium | low
updated_at
```

Unknown capability does not always mean unsupported. Policy determines whether unknown metadata is acceptable for a request.

---

## 12. CLIProxyAPI Integration

CLIProxyAPI is an **optional bridge**, not a mandatory routing layer.

### 12.1 Correct integration boundary

```text
                    Smart Router
                         |
        +----------------+----------------+
        |                |                |
        v                v                v
 Generic Drivers    Native Drivers   CLIProxy Bridge
        |                |                |
 direct APIs         special APIs     OAuth/subscription
```

Direct APIs should connect directly whenever a generic or native Smart Router driver can manage them safely.

CLIProxyAPI is valuable for provider classes where it already handles difficult OAuth/CLI subscription lifecycle and account pooling.

### 12.2 1.0 mode: Managed Pool

In 1.0, CLIProxy is treated as a schedulable pool-level resource.

```text
Smart Router chooses: CLIProxy pool / model
CLIProxy chooses: internal account/credential
```

Smart Router tracks:

- pool-level health;
- local usage telemetry;
- observed failures;
- rate-limit/cooldown signals visible through responses;
- optional management metadata when available.

Smart Router must not assume that CLIProxy management metadata exposes exact per-account runtime quota.

### 12.3 Later mode: Transparent Accounts

Post-1.0 work may allow Smart Router to see and schedule individual CLIProxy accounts if stable management/runtime telemetry becomes available.

Do not make 1.0 depend on this.

### 12.4 CLIProxy management integration

If enabled:

- management credentials are stored separately from data-plane credentials;
- management integration is version-aware;
- a management API failure must not bring down data-plane traffic when the primary CLIProxy endpoint is healthy;
- compatible custom providers may optionally be imported into direct Smart Router connections.

---

## 13. ProxyPal Boundary

ProxyPal is **not** a runtime dependency of Smart Router.

Useful ideas to reuse conceptually include:

- provider/account onboarding UX;
- request monitoring;
- provider-specific quota collector patterns;
- visual quota windows and reset information.

A user may continue running ProxyPal to manage CLIProxy, but Smart Router owns global routing decisions and its own usage ledger.

---

## 14. Gateway and Protocol Compatibility

### 14.1 Required endpoints

| Protocol | Endpoint | 1.0 priority |
|---|---|---|
| Anthropic | `/v1/messages` | MUST |
| Anthropic | `/v1/messages/count_tokens` | MUST |
| Common | `/v1/models` | MUST |
| OpenAI | `/v1/chat/completions` | SHOULD |
| OpenAI | `/v1/responses` | SHOULD |
| Health | `/health/live` | MUST |
| Health | `/health/ready` | MUST |

### 14.2 Compatibility philosophy

Do not claim generic compatibility merely because JSON shapes look similar.

For every supported endpoint:

- define a protocol adapter;
- define translation behavior;
- define streaming behavior;
- define error mapping;
- define usage extraction;
- cover behavior with contract fixtures.

Unsupported semantics should be explicit rather than silently dropped.

---

## 15. Universal Quota Engine

Quota is a **multi-dimensional constraint graph**, not a token counter.

### 15.1 QuotaResource

Suggested model:

```python
@dataclass
class QuotaResource:
    id: str
    scope_type: str
    scope_id: str
    metric: str
    window: str | None
    capacity: Decimal | None
    used: Decimal | None
    reserved: Decimal
    remaining: Decimal | None
    reset_at: datetime | None
    source: str
    confidence: str
    shared_group_id: str | None
    hard_limit: bool
    safety_buffer: Decimal
    updated_at: datetime
```

### 15.2 Supported metrics

The model must not assume tokens are the only quota unit.

Examples:

```text
request
input_token
output_token
total_token
cached_token
credit
usd
interaction
compute_unit
neuron
concurrent_request
percentage/custom provider unit
```

### 15.3 Scope types

Quota may apply to:

```text
provider
connection
credential
project
model
model family
subscription
shared account group
```

### 15.4 Shared quota groups

If several models share one account quota, do not create independent capacity copies for each model.

Instead:

```text
Model A ----+
Model B ----+--> SharedQuotaGroup(account-weekly-123)
Model C ----+
```

All relevant resources depend on the same constraint.

### 15.5 Constraint graph

A `ModelResource` can depend on multiple hard and soft constraints.

Example:

```text
ModelResource X
  -> project.daily_tokens
  -> connection.requests_per_minute
  -> credential.tokens_per_minute
  -> account.weekly_credits
  -> credential.concurrent_requests
```

Eligibility rule:

```text
ALL hard constraints must pass.
Soft constraints contribute pressure/penalty to scheduling.
```

### 15.6 Quota source and confidence

Possible `source` values:

```text
provider_api
response_header
response_usage
management_api
configured
local_counter
estimated
inferred
```

Possible `confidence` values:

```text
exact
high
medium
low
```

Preferred source order is generally:

1. verified provider quota/balance API;
2. verified response rate-limit headers;
3. provider response usage;
4. configured internal caps;
5. local provider-specific estimator;
6. generic estimator or inferred reset state.

The actual ordering can differ per metric because provider semantics differ. The driver or collector owns that interpretation.

### 15.7 Separate rate limits, quota and health

Do not model every `429` as the same cooldown.

```text
Health:
  timeout / connection failure / 5xx

Rate limit:
  RPM / TPM / concurrency

Quota:
  daily / weekly / monthly / credits / subscription capacity

Auth:
  expired / revoked / invalid
```

Different states require different scope and recovery behavior.

### 15.8 Atomic reservation

Before sending a request, reserve expected capacity in Redis.

Conceptually:

```text
effective_remaining = capacity - used - reserved - safety_buffer
required = expected_consumption + risk_buffer
```

A single atomic Redis operation or Lua script must:

1. read all required hard constraints;
2. verify they can satisfy the reservation;
3. increment all reservations/concurrency counters;
4. fail the entire reservation if any hard constraint cannot pass.

Never partially reserve a multi-constraint request.

### 15.9 Reconciliation

After upstream completion:

- replace estimates with actual usage where available;
- release unused reservation;
- record overshoot when actual usage exceeds reservation;
- update estimator/safety settings from aggregate history;
- update rate-limit fields from verified response headers;
- account for uncertain consumption when a failed request may already have reached upstream.

Reservation and reconciliation must be idempotent by request/attempt identity.

---

## 16. Usage Ledger and Token Accounting

The ledger must tell the truth about what is known and what is estimated.

### 16.1 Usage source priority

Prefer:

1. provider response usage metadata;
2. provider tokenizer/count endpoint;
3. model-specific local tokenizer/estimator;
4. generic estimation.

The last two must be marked estimated.

### 16.2 UsageEvent

Suggested shape:

```text
request_id
attempt_id
provider_connection_id
credential_id
model_resource_id
input_tokens
output_tokens
cached_input_tokens
cache_write_tokens
reasoning_tokens
total_tokens
native_metric
native_amount
actual_cost
currency
source
confidence
estimated
observed_at
```

Not every provider supplies every field. Null is acceptable. False precision is not.

### 16.3 Request versus Attempt

One client request may create several upstream attempts.

```text
Request R1
  -> Attempt A1: resource X -> RATE_LIMIT
  -> Attempt A2: resource Y -> TRANSIENT_NETWORK
  -> Attempt A3: resource Z -> success
```

The system must retain both levels.

Request-level totals include all upstream attempts that may have consumed capacity, not just the final successful one.

### 16.4 Streaming usage

Streaming uses a tap pattern:

```text
Upstream stream
     |
     +-------> Client immediately
     |
     +-------> Usage/event parser
```

Do not buffer the entire response merely to compute usage.

### 16.5 Privacy

By default, persist metadata, not prompt/response content.

Do not store by default:

- raw prompt bodies;
- raw response bodies;
- Authorization headers;
- provider secrets;
- browser cookies;
- CLIProxy management keys.

Optional debug payload capture must be explicit, access-controlled, masked and short-lived.

---

## 17. Error Classification

Normalize provider failures to a stable internal error taxonomy.

Suggested kinds:

| Error kind | Typical scope | Default action |
|---|---|---|
| `RATE_LIMIT` | credential/model/connection | cooldown until reset, then retry elsewhere |
| `QUOTA_EXHAUSTED` | account/subscription/project | block until reset/replenishment |
| `AUTH_EXPIRED` | credential | refresh if supported, temporarily disable |
| `AUTH_REVOKED` | credential | disable + alert |
| `MODEL_NOT_FOUND` | model | mark stale and trigger discovery |
| `OVERLOADED` | model/provider | short backoff + retry elsewhere |
| `CONTEXT_TOO_LARGE` | request/model | do not retry on smaller-context model |
| `INVALID_REQUEST` | request | return to client; do not open generic circuit |
| `CONTENT_POLICY` | request/provider | policy-specific, usually not generic retry |
| `TRANSIENT_NETWORK` | connection | retry within budget |
| `UNKNOWN` | attempt | conservative backoff + observability |

The classifier should also return:

```text
scope
retryable
retry_after/reset_at
consumption_uncertainty
provider_error_code
safe_message
```

---

## 18. Smart Scheduler and Free Capacity Optimizer

The 1.0 scheduler must be deterministic, testable and explainable.

### 18.1 Required pipeline

```text
Eligibility
   -> Resource constraints
   -> Consumption estimate
   -> Feature scoring
   -> Atomic reservation
   -> Execute
   -> Error classification / retry
   -> Reconciliation
```

### 18.2 Eligibility filters

Hard-filter a candidate when appropriate for any of the following:

- disabled resource;
- unsupported protocol;
- unsupported tool/vision/structured-output capability;
- context too small;
- output limit too small;
- invalid/revoked credential;
- open hard circuit;
- hard quota unavailable;
- concurrency unavailable;
- project budget exhausted;
- paid fallback disallowed;
- model deprecated or unavailable.

### 18.3 Scoring features

Recommended normalized features for 1.0:

| Feature | Meaning |
|---|---|
| `capability_fit` | How well the model supports this request |
| `quality_fit` | Admin/benchmark quality score relative to policy |
| `free_savings` | Monetary spend avoided by using this resource |
| `quota_headroom` | Remaining capacity after reservation |
| `expiry_urgency` | Risk of unused capacity expiring/resetting |
| `reliability` | Recent success/error behavior |
| `latency` | P50/P95/first-byte cost |
| `cache_locality` | Benefit of staying on current resource/session |
| `scarcity` | Opportunity cost of consuming a hard-to-replace resource |
| `retry_cost` | Expected repeated input/output caused by failures |
| `uncertainty` | Penalty for low-confidence metadata |
| `paid_cost` | Expected monetary cost |

Illustrative model:

```text
score =
    w_capability * capability_fit
  + w_quality * quality_fit
  + w_free * free_savings
  + w_headroom * quota_headroom
  + w_expiry * expiry_urgency
  + w_reliability * reliability
  + w_cache * cache_locality
  - w_latency * latency_penalty
  - w_scarcity * scarcity
  - w_retry * expected_retry_cost
  - w_uncertainty * uncertainty
  - w_paid * expected_paid_cost
```

Do not hardcode one global coefficient set. Weights belong to `RoutingPolicy`.

### 18.4 Burn-rate and expiry urgency

When reset semantics are trustworthy:

```text
effective_remaining = reported_remaining - reserved - safety_buffer
seconds_to_reset = max(reset_at - now, 1)
target_burn_rate = effective_remaining / seconds_to_reset
```

If observed burn is far below target, free capacity may be wasted at reset and `expiry_urgency` may increase.

If observed burn is far above target, reduce preference to protect capacity.

If reset semantics are unknown, do not invent a reset time. Set expiry urgency to zero or confidence-weight it conservatively.

### 18.5 Scarcity and shadow price

Free capacity still has opportunity cost.

A scarce high-quality model that is uniquely capable of a workload should not be consumed for trivial work merely because its monetary price is zero.

Use a scheduler-only scarcity/shadow-price signal. Never record this internal value as actual provider cost in the ledger.

### 18.6 Retry economics

For large prompts, retrying another provider may repeat significant input usage.

Use expected retry cost, for example:

```text
expected_retry_cost ~= failure_probability * expected_repeated_consumption
```

A nominally free provider with frequent failures may be worse than a more reliable free provider.

---

## 19. Policy Presets

Provide configurable presets, not hardcoded algorithms.

Suggested initial presets:

### `auto-free`

Prioritize legitimate free capacity while maintaining a minimum quality floor.

Primary signals:

- free savings;
- quota headroom;
- expiry urgency;
- reliability;
- capability fit;
- retry cost;
- scarcity.

Paid fallback is allowed only if explicitly configured and budget permits it.

### `fast`

Prioritize:

- first-byte latency;
- completion latency;
- reliability;
- adequate quality.

### `coding`

Prioritize:

- coding quality;
- tool support;
- context;
- cache locality/session affinity;
- reliability;
- free savings.

### `review`

Prioritize:

- reasoning/quality;
- reliability;
- context;
- scarcity awareness;
- conservative retry budget for large prompts.

### `critical`

Prioritize quality and reliability over cost, but still enforce explicit administrative cost ceilings.

### Example policy data

```yaml
name: auto-free

constraints:
  enabled: true
  min_quality: 0.65
  require_capabilities: auto
  allow_paid_fallback: true
  max_expected_cost_per_request: 0.10
  min_quota_headroom: 0.03

weights:
  free_savings: 1.00
  quality_fit: 0.70
  reliability: 0.55
  quota_headroom: 0.45
  expiry_urgency: 0.35
  cache_locality: 0.30
  latency: -0.20
  retry_cost: -0.45
  scarcity: -0.35
  uncertainty: -0.25

retry:
  max_attempts: 3
  max_extra_input_tokens: 60000

reservation:
  safety_buffer_ratio: 0.05
```

These numbers are implementation defaults for simulation only. They are not universally optimal constants.

---

## 20. Session Affinity and Cache Locality

Long coding and conversational sessions can waste tokens if routing moves arbitrarily between providers/resources.

### 20.1 Session binding

When a stable session hint exists, store a TTL binding:

```text
session -> current ModelResource
```

Keep the binding when:

- resource is healthy;
- quota is sufficient;
- policy still allows the resource;
- capability remains valid.

Rebind when:

- auth becomes invalid;
- quota is exhausted;
- circuit is blocked;
- model is deprecated;
- capability mismatch occurs;
- policy/budget changes materially.

Affinity is normally a scoring bonus, not a hard lock.

### 20.2 Cache accounting

Where provider metadata permits it, track:

```text
input_tokens
cached_input_tokens
cache_write_tokens
output_tokens
reasoning_tokens
```

Historical cache locality may feed the scheduler.

---

## 21. Retry Budget

Retry is a resource decision, not just an error-handling loop.

A policy should support at least:

```yaml
retry_policy:
  max_attempts: 3
  max_extra_input_tokens: 60000
  max_extra_latency_ms: 5000
  retryable_errors:
    - RATE_LIMIT
    - OVERLOADED
    - TRANSIENT_NETWORK
```

Never automatically retry invalid requests, revoked credentials, or other non-retryable failures merely because another provider exists.

---

## 22. Control Plane and Admin API

The admin plane must use separate authentication/scopes from data-plane router API keys.

Suggested namespace:

```text
/api/admin/v1
```

### 22.1 API groups

| Group | Example operations |
|---|---|
| Providers | CRUD providers, test, sync models |
| Credentials | add, rotate, disable, test |
| Models/Resources | list, enable/disable, override metadata |
| Routes/Policies | CRUD, simulation |
| Quota | current state, observations, configured caps |
| Usage | requests, attempts, summaries, costs |
| Config | revisions, publish, rollback |
| CLIProxy | test, sync metadata, import compatible config |
| Projects | project/API-key/budget/concurrency settings |

### 22.2 Required UI screens

#### Overview

Show:

- request volume;
- success rate;
- free-capacity utilization;
- paid fallback usage/cost;
- failover rate;
- P50/P95 latency;
- quota pressure and alerts.

#### Providers

Show:

- connection status;
- credentials and states;
- health;
- test connection;
- model sync;
- Add Provider wizard.

#### Models / Resources

Show:

- physical model;
- connection;
- capability metadata;
- price metadata;
- quota pressure;
- health;
- enabled state.

#### Routes / Policies

Allow administrators to configure:

- candidate selectors;
- capability/quality constraints;
- cost/latency/free-capacity weights;
- paid fallback policy;
- retry budget;
- quota reserve thresholds.

#### Usage / Requests

Allow request -> attempt inspection including:

- selected resource;
- retry/failover chain;
- status and normalized error;
- token/cost accounting;
- latency;
- routing decision metadata.

Prompt bodies remain hidden/not persisted by default.

#### Settings / Security

Include:

- project API keys;
- retention;
- encryption/master-key status;
- CLIProxy integration;
- config revisions;
- audit history;
- network/SSRF policy.

### 22.3 Route simulation

Before publishing a policy, support dry-run simulation.

Input example:

```text
route = coding
estimated_input_tokens = 45000
max_output_tokens = 6000
tools = true
vision = false
session = example-session
```

Output should show:

- candidates considered;
- eligibility decision;
- failed constraints;
- normalized features;
- score;
- expected reservation;
- selected resource;
- reason.

No provider call is made during simulation.

---

## 23. Configuration Revisions and Hot Reload

Never have the UI directly mutate live routing objects.

Activation sequence:

1. Admin changes configuration.
2. A draft revision is created.
3. Validator checks schema and references.
4. Validator checks route cycles and policy consistency.
5. Validator checks required secret references and driver compatibility.
6. Compiler builds an immutable `RuntimeConfigSnapshot`.
7. If valid, mark the revision active.
8. Publish a Redis config-change event.
9. Gateway instances atomically swap snapshots.
10. In-flight requests finish on their old snapshot.

If validation fails, the previous active revision remains live.

Rollback activates a previous valid revision. It does not mean rolling back database migrations.

---

## 24. Database Model

PostgreSQL is the durable source of truth.

Core tables should include at least:

| Table | Purpose |
|---|---|
| `provider_definitions` | driver/protocol/template metadata |
| `provider_connections` | concrete endpoints/network settings |
| `provider_credentials` | encrypted secret references and credential state |
| `model_definitions` | provider model metadata/capabilities/pricing |
| `model_resources` | schedulable connection + credential scope + model identity |
| `quota_resources` | quota constraints and shared groups |
| `quota_observations` | provider/header/local/inferred observations over time |
| `quota_reservations` | reservation lifecycle/audit if persisted |
| `logical_routes` | public route names |
| `routing_policies` | constraints, weights and retry/budget rules |
| `requests` | client-level outcomes and totals |
| `request_attempts` | individual upstream execution records |
| `usage_events` | append-only token/native-unit/cost observations |
| `config_revisions` | versioned configuration snapshots/changes |
| `audit_logs` | administrative/security events without secrets |
| `projects` | downstream project limits and settings |
| `router_api_keys` | hashed/scoped project data-plane credentials |

### 24.1 ID and history rules

- Prefer UUID or ULID identifiers.
- Do not use provider model name as primary key.
- Keep provider/model history through disabled/deprecated state.
- Keep `UsageEvent` append-only.
- Aggregates should be rebuildable from retained events where feasible.
- Never include raw secret values in audit diffs.

---

## 25. Redis Runtime State

Redis is not the durable source of truth, but it is required for distributed coordination.

Example keys are conceptual; final key names may differ:

```text
quota:{quota_resource_id}
reservation:{attempt_id}
concurrency:{scope_id}
circuit:{scope_type}:{scope_id}
ratelimit:{scope_type}:{scope_id}
session:{session_hash}
config:active_revision
```

Use atomic scripts/transactions for operations that span multiple quota constraints.

The system should be able to rebuild runtime state from durable configuration/observations after restart where practical.

---

## 26. Security Requirements

Security is part of the core architecture, not a final hardening task.

### 26.1 Secret storage

1.0 target:

```text
MASTER_KEY / KMS-like root
        |
        v
Envelope encryption / AES-GCM
        |
        v
Encrypted provider credentials in PostgreSQL
```

Requirements:

- secrets are encrypted at rest;
- UI shows only fingerprint/last4 after saving;
- no API endpoint returns the full stored secret;
- secret rotation is auditable without logging secret contents;
- legacy environment references may temporarily remain during migration.

### 26.2 Application security

Implement:

- owner/admin/viewer RBAC at minimum;
- data-plane versus admin-plane scopes;
- secure browser session/cookies;
- CSRF protection for browser admin flows;
- CORS allowlist;
- request body size limits;
- explicit timeouts;
- secure TLS verification by default;
- audit logging for security-sensitive configuration;
- rate limits for downstream router API keys where appropriate.

### 26.3 SSRF protection

Custom base URLs create SSRF risk.

The system needs a network policy that can:

- validate URL scheme and host;
- block cloud metadata endpoints;
- block private/local networks by default in exposed deployments;
- explicitly allow local/self-hosted model networks when configured;
- log/audit unsafe overrides.

### 26.4 Existing `.env` migration prerequisite

The original source archive contains `.env`. Before publishing or creating a public repository:

- remove `.env` from distributable artifacts;
- ensure `.gitignore` excludes real secret files;
- review repository/history if it has already been shared;
- rotate any credentials that may have been exposed.

---

## 27. Observability

### 27.1 Required metrics

Track at minimum:

```text
requests_total
request_success_rate
request_latency_p50/p95/p99
first_byte_latency
attempts_per_request
failover_rate
retry_token_overhead
input/output/cached/reasoning tokens
quota pressure / remaining / reservation mismatch
quota overshoot count
429 / 5xx / auth errors by scope
paid_fallback_cost
free_capacity_utilization
provider discovery freshness
quota sync freshness
circuit open count
credential disabled count
concurrency saturation
```

### 27.2 Structured logs

Typical event fields:

```text
request_id
project_id
logical_route
attempt_id
model_resource_id
provider_connection_id
model_id
status
error_kind
latency_ms
first_byte_ms
usage_source
input_tokens
output_tokens
quota_decision
failover_reason
```

Never include Authorization headers, secret values, raw prompt body, or management keys by default.

### 27.3 Reliability targets

1.0 targets/guardrails:

- gateway internal P95 overhead below approximately 50 ms excluding provider network latency;
- invalid config revision never replaces the active snapshot;
- restarting the service does not lose durable usage/config truth;
- streaming never buffers the entire response merely for accounting;
- every client request receives a request ID;
- every upstream call should have an attempt record when durable storage is available;
- database/Redis degraded behavior must be explicit, configurable and observable.

---

## 28. Deployment Model

### 28.1 Technology baseline

| Layer | Technology |
|---|---|
| Gateway / Worker | Python 3.12+, FastAPI, asyncio, httpx |
| ORM / migrations | SQLAlchemy 2 async + Alembic |
| Durable data | PostgreSQL |
| Runtime coordination | Redis |
| Control Plane web | React + TypeScript + Vite |
| Self-hosted packaging | Docker Compose |

### 28.2 Service topology

```text
docker compose
  gateway     FastAPI data/admin API
  worker      discovery/quota/health/aggregation jobs
  web         React static application
  postgres    durable configuration + ledger
  redis       runtime coordination
```

Production can later scale to multiple gateway/worker instances with managed PostgreSQL/Redis and a reverse proxy/TLS layer.

### 28.3 Worker model

Do not introduce Celery solely by default.

1.0 can use an asyncio worker process with:

- scheduled jobs;
- Redis leases/distributed locks;
- idempotent job semantics.

Initial jobs:

```text
model discovery sync
quota sync
health probe
usage aggregate rollup
retention cleanup
```

Introduce a heavier queue only if workload characteristics justify it.

---

## 29. Target Repository Structure

Target structure after migration:

```text
smart-router/
  apps/
    gateway/
      api/
      protocols/
      routing/
        engine.py
        capability.py
        planner.py
        scheduler.py
        policies.py
      providers/
        base.py
        registry.py
        generic_openai.py
        generic_anthropic.py
        generic_gemini.py
        cliproxy_bridge.py
      quota/
        engine.py
        reservation.py
        models.py
      usage/
        collector.py
        models.py
      security/
      db/
    worker/
      jobs/
      collectors/
      aggregation/
    web/
      src/
  migrations/
  templates/
    providers/
  tests/
    unit/
    contract/
    integration/
    e2e/
    fixtures/
  docker/
  docs/
  docker-compose.yml
  README.md
```

Concrete providers such as AI-BOX, xKiro, Groq, Gemini or OpenRouter must not appear in `routing/` core. If they require special handling, place that code in a provider template, provider-specific driver, collector, bridge or compatibility module.

---

## 30. Migration from the Current Codebase

Migration must be incremental.

### 30.1 Legacy mapping

| Current concept | Target concept | Migration approach |
|---|---|---|
| `upstreams.{name}` | `ProviderConnection` | move URL/network/auth metadata to connection + credential |
| `token_env` | secret reference | support env reference first, encrypted DB target later |
| `Candidate(upstream, model)` | `ResourceCandidate` / selector | resolve connection + model; credential selected by resource layer |
| `weight` | policy hint | retain as tie-break/static feature |
| `fallback` | secondary/paid fallback tier | preserve existing ordering semantics |
| in-memory `CircuitState` | Redis-backed scoped state | model/credential/connection-aware keys |
| AI-BOX catalog sync | generic discovery/collector | move out of routing core |
| AI-BOX promotion logic | data-driven `RoutingPolicy` | replace provider-specific promotion rules |
| `router_status` | resource/route status API | keep compatibility alias during migration |

### 30.2 Legacy compiler

During migration, startup should support:

```text
if database has active config revision:
    load ResourceRegistry snapshot
elif legacy config.yaml exists:
    parse legacy config
    compile transient ResourceRegistry snapshot
    emit migration warning
else:
    fail configuration validation
```

Provide an explicit migration command, for example:

```bash
smart-router migrate legacy-config config.yaml
```

### 30.3 Refactor order

Perform migration in this sequence:

1. Extract `RouterEngine` from FastAPI handlers while preserving current behavior.
2. Replace old `Candidate` with `ResourceCandidate` / `ResourceRef`.
3. Add a legacy config compiler to the new resource model.
4. Move upstream URL/auth/headers into generic driver code.
5. Extract circuit-state interface and add Redis-backed implementation.
6. Move AI-BOX discovery/promotion out of router core into templates/collectors/compatibility code.
7. Add PostgreSQL `ResourceRegistry` and config revision compiler.
8. Add generic onboarding/UI.
9. Add Usage Ledger and Universal Quota Engine.
10. Add CLIProxy Bridge.
11. Add Smart Scheduler behind a feature flag.
12. Complete Control Plane and production hardening.

### 30.4 Legacy routing fallback

Until 1.0 is stable, support a feature flag conceptually similar to:

```text
LEGACY_ROUTING_MODE=true
```

Legacy mode should use the **new ResourceRegistry** with old priority/weighted behavior. It must not revert to provider hardcoding.

This gives a rollback path for scheduler/quota regressions without undoing the architecture migration.

---

## 31. Testing Strategy

### 31.1 Test pyramid

| Test type | Required coverage |
|---|---|
| Unit | scoring, quota math, classifier, manifests, migration compiler, usage parsers |
| Contract | protocol translation, streaming, errors, usage parsing using fixtures |
| Integration | PostgreSQL + Redis + fake upstreams |
| Concurrency | atomic reservation, release, reconciliation and oversubscription prevention |
| Chaos | 429, reset headers, 5xx, timeout, stream drop, stale metadata, datastore interruptions |
| E2E | add provider -> route -> gateway request -> usage/quota visible in UI/API |
| Migration | legacy config compiles to equivalent routing semantics |
| Security | secret redaction, scopes, SSRF, CSRF/CORS, invalid manifests |

### 31.2 Mandatory invariant tests

The test suite must explicitly verify:

- fallback resources are not mixed into the primary weighted pool unless policy permits it;
- invalid request / 401-like client failures do not open generic provider circuits;
- `Retry-After`/verified reset metadata overrides static cooldown defaults;
- streaming never transparently fails over after first output;
- multi-constraint reservation is all-or-nothing;
- concurrent reservations do not oversubscribe hard quota;
- shared quota groups are not counted independently for every model;
- estimated usage is never marked as exact provider truth;
- invalid config revisions never replace active runtime config;
- secrets do not appear in logs, audit diffs, diagnostics or API responses;
- management API failure does not unnecessarily break CLIProxy data-plane traffic;
- provider-specific branches do not appear in routing core.

### 31.3 CI quality gate

A merge to `main` should require:

- formatting/lint;
- type checking;
- unit tests;
- contract tests;
- integration tests relevant to the change;
- Alembic migration smoke test when schema changes;
- secret scan;
- legacy migration smoke test while legacy support exists.

### 31.4 Implementation progress tracking

Every implementation step must be recorded in `docs/notes/implementation-progress.md` as the work proceeds. Each note should include the date, what changed, verification run, test outcome, and any follow-up risks or TODOs. This progress note is the project handoff log for future humans and agents, so do not rely on chat history as the source of truth.

### 31.5 Mandatory `claude-router-review` checks

This is a **hard gate** for all implementation work. No code changes shall be committed or integrated without passing through the `claude-router-review` model verification at both stages described below. If `claude-router-review` cannot be invoked due to environment constraints (unrecognized model, permission deny, tooling misconfiguration), the block remains in effect until the reviewer becomes available — do not bypass or skip the check under any circumstances.

#### Pre-implementation plan review

After analysis and planning, use model `claude-router-review` to review whether the plan is optimized, appropriately scoped, and aligned with the architecture contract in this README before implementation begins. The plan review must explicitly confirm:

- correctness of architectural boundaries (core vs driver vs compatibility layer);
- absence of circular import risk;
- correct handling of sync/async ledger operations via `inspect.isawaitable`;
- data-plane safety (no exception from usage parsing or driver resolution can propagate to HTTP response path);
- test coverage completeness for success, failure, and edge-case scenarios.

Record the outcome in `docs/notes/implementation-progress.md` together with any conditions or required modifications. Do not begin coding until the review returns approved status.

#### Post-implementation code review

After each coding step (not just at milestones), use model `claude-router-review` to verify whether the produced code is correct, simple, secure, and architecture-consistent before moving to the next step. Record the review outcome in `docs/notes/implementation-progress.md` alongside normal verification notes. If `claude-router-review` reports any verified blockers, fix them immediately before proceeding.

A release candidate additionally requires:

- Docker Compose E2E from an empty database;
- Docker Compose E2E from legacy migration path;
- concurrency/load test;
- security test suite;
- backup/restore validation.

---

## 32. Implementation Roadmap

Milestones are dependency ordered. Do not implement later optimizer features by bypassing earlier telemetry/resource work.

### M0 - Harden baseline

Implement:

- remove secrets from artifacts;
- request IDs and structured logging;
- Docker baseline;
- CI;
- stream regression tests;
- preserve current test behavior.

Exit condition:

> Existing router is reproducible and safe enough to refactor.

### M1 - Provider Registry Core

Implement:

- domain entities;
- legacy config compiler;
- extracted `RouterEngine`;
- `ResourceCandidate` abstraction;
- `ProviderDriver` interface;
- PostgreSQL + SQLAlchemy/Alembic baseline;
- scoped circuit repository interface.

Exit condition:

> Routing core no longer hardcodes AI-BOX, xKiro or ProxyPal behavior.

### M2 - Generic onboarding

Implement:

- OpenAI-compatible driver;
- Anthropic-compatible driver;
- Gemini-compatible driver;
- provider templates;
- add/test/discover provider APIs;
- config revision validation/activation;
- minimal Provider/Model UI.

Exit condition:

> A user can add most compatible API providers without editing source code or restarting the gateway.

### M3 - Usage Ledger + Quota Engine

Implement:

- Request / Attempt / UsageEvent ledger;
- usage parsers;
- Redis runtime state;
- quota graph;
- atomic reservation;
- reconciliation;
- error classifier;
- rate-limit versus quota state separation.

Exit condition:

> Quota-aware admission control works under concurrent requests.

### M4 - CLIProxy Bridge

Implement:

- managed-pool data path;
- optional management metadata integration;
- pool-level health/quota telemetry;
- compatible provider import where feasible;
- no ProxyPal runtime dependency.

Exit condition:

> OAuth/subscription pools managed by CLIProxy can participate in Smart Router routing.

### M5 - Smart Scheduler

Implement:

- capability eligibility;
- policy constraints;
- normalized deterministic scoring;
- quota headroom;
- burn-rate/expiry urgency;
- scarcity;
- reliability;
- expected retry cost;
- paid ceilings;
- session/cache affinity where feasible;
- route simulation.

Exit condition:

> Routing is resource-aware and measurably better than reactive priority/fallback for free-capacity utilization without violating quality/policy constraints.

### M6 - Complete Control Plane

Implement:

- Overview;
- request explorer;
- usage/quota views;
- route/policy editor;
- alerts;
- project/API-key management;
- security settings;
- audit history;
- revision rollback.

Exit condition:

> Normal system administration no longer requires manual YAML editing.

### M7 - 1.0 hardening

Implement:

- load/concurrency tests;
- chaos tests;
- security verification;
- backup/restore procedures;
- operational docs;
- migration tooling;
- SLO verification.

Exit condition:

> Smart Router is production-ready for a self-hosted 1.0 deployment.

---

## 33. Version 1.0 Acceptance Criteria

The release is complete only when all applicable criteria pass.

- **AC-01** A user can add an OpenAI-compatible provider from the UI, test it, discover/import models and route traffic to it without source edits or gateway restart.
- **AC-02** `RouterEngine` contains no provider-name branches for AI-BOX, xKiro, ProxyPal, Groq, Gemini, OpenRouter or equivalent concrete providers.
- **AC-03** One connection can contain multiple credentials with independent state/quota/concurrency where supported.
- **AC-04** Every request has a request ID and every upstream call has an attempt record; usage/cost provenance is explicit.
- **AC-05** `QuotaResource` supports request, token, credit/currency and concurrency-style metrics, multiple windows and shared groups.
- **AC-06** Concurrent reservation cannot oversubscribe configured hard quota in integration tests.
- **AC-07** Rate-limit resets and long-term quota exhaustion are represented as different runtime states.
- **AC-08** Router rejects resources that fail capability, context, quota, auth, concurrency or budget requirements.
- **AC-09** Scheduler provides at least `auto-free`, `fast`, `coding`, `review` and `critical` policy presets.
- **AC-10** Paid fallback is only used when policy allows and configured budget ceilings pass.
- **AC-11** Streaming failover is only possible before first output and is regression tested.
- **AC-12** CLIProxy managed-pool mode works without ProxyPal and remains usable when optional management integration is unavailable.
- **AC-13** Control Plane contains Providers, Models/Resources, Routes/Policies, Usage/Requests, Overview and Settings/Security.
- **AC-14** Credentials are encrypted at rest and never returned/logged in plaintext after storage.
- **AC-15** Docker Compose starts gateway, worker, web, PostgreSQL and Redis and has documented backup/restore.
- **AC-16** Legacy `config.yaml` can be compiled/migrated while preserving important logical route behavior during the compatibility window.

---

## 34. Architectural Decisions (ADR)

These are baseline decisions for 1.0.

| ID | Decision | Reason |
|---|---|---|
| ADR-001 | Keep Python/FastAPI for gateway 1.0 | Avoid rewrite risk and preserve working async code |
| ADR-002 | PostgreSQL durable truth, Redis runtime coordination | Separate durable data from high-frequency ephemeral state |
| ADR-003 | ProviderDefinition/Connection/Credential/ModelResource is the standard resource model | Enables provider agnosticism and multi-credential scheduling |
| ADR-004 | Direct generic drivers; CLIProxy is optional bridge | Preserve visibility when possible and reuse OAuth lifecycle when necessary |
| ADR-005 | ProxyPal is not a runtime dependency | Avoid coupling to a desktop/control application |
| ADR-006 | Declarative manifests cannot execute arbitrary code | Limit attack surface and simplify operations |
| ADR-007 | Quota is a multidimensional constraint graph | Supports shared and heterogeneous provider quotas |
| ADR-008 | RoutingEnvelope canonicalizes metadata, not the full payload | Avoid protocol semantic loss |
| ADR-009 | No cross-model streaming failover after first output | Preserve response correctness |
| ADR-010 | 1.0 optimizer is deterministic and explainable | Allows testing, debugging and policy simulation |
| ADR-011 | React + TypeScript + Vite for the self-hosted control plane | Minimize operational complexity |
| ADR-012 | Legitimate resource management only | Product must not be designed to circumvent provider restrictions |

Changing an ADR requires an explicit replacement ADR and corresponding updates to tests, migration plan and this README.

---

## 35. Known Risks and Required Mitigations

| Risk | Required mitigation |
|---|---|
| Provider semantics change | versioned drivers/templates, contract fixtures, last-known-good metadata |
| Provider does not expose quota | source/confidence, local counters, conservative safety buffers |
| CLIProxy hides account-level state | treat it as one managed pool in 1.0 |
| Concurrent oversubscription | atomic Redis multi-constraint reservation |
| Retry burns free capacity | retry token/time budgets and retry-cost scoring |
| DB/Redis outage | explicit degraded mode, readiness, no silent accounting loss |
| Custom endpoint SSRF | network validation and explicit local allowlists |
| Scope creep | MUST/SHOULD/LATER and milestone exit conditions |
| Plugin supply-chain risk | no public arbitrary-code plugin marketplace in 1.0 |
| Model quality is subjective | explicit admin/benchmark quality metadata and minimum floors |

---

## 36. Post-1.0 Backlog

Do not make these requirements implicit dependencies of current milestones:

- transparent account-level CLIProxy scheduling;
- out-of-process provider plugin SDK;
- signed plugin packages and compatibility kits;
- time-of-day/day-of-week free-capacity forecasting;
- adaptive output/retry estimators;
- richer quality/benchmark registry;
- multi-region routing;
- enterprise SSO/RBAC;
- external KMS/Vault integrations;
- signed remote provider-template registry;
- advanced chargeback/showback;
- richer OpenTelemetry/Prometheus export;
- full SaaS billing.

---

## 37. PR Definition of Done

A pull request is not complete only because the happy path works.

### Routing-core PR

Must:

- preserve provider agnosticism;
- add tests for affected routing invariants;
- avoid unrelated provider-specific conditionals;
- document any changed policy semantics.

### Provider-driver PR

Must include fixtures/tests for:

- connection validation;
- normal success;
- streaming;
- auth error;
- invalid request;
- rate limit;
- transient 5xx/network failure;
- usage parsing;
- model discovery where supported.

### Quota PR

Must include:

- schema/migration if applicable;
- unit quota math tests;
- integration reservation tests;
- concurrency test;
- shared-quota test;
- reconciliation/overshoot test.

### Admin/config PR

Must include:

- validation;
- revision creation;
- audit event;
- invalid-config non-activation test;
- RBAC/scope test when applicable.

### Secret/security PR

Must include:

- negative leak tests;
- log/audit redaction tests;
- secret scan;
- secure defaults.

### Legacy migration PR

Must compare:

- logical routes;
- primary ordering;
- fallback ordering;
- important config semantics;
- legacy versus new runtime behavior using fixtures.

---

## 38. Recommended First Implementation Tasks

When starting from the current source, do not begin by integrating more providers.

Recommended initial work sequence:

1. Remove `.env` from distributable project state and rotate exposed credentials.
2. Add structured request IDs without changing routing behavior.
3. Extract FastAPI handler logic into a testable `RouterEngine` service.
4. Introduce `ResourceRef` / `ResourceCandidate` while compiling existing YAML into the new representation.
5. Introduce `ProviderDriver` and move existing URL/auth execution behind it.
6. Move provider-specific catalog behavior out of routing core.
7. Add PostgreSQL models and Alembic.
8. Add config revision compiler and immutable runtime snapshot.
9. Add Redis-backed scoped circuit repository.
10. Only then start generic provider onboarding.

The first architectural success condition is:

> **A new provider can be added without editing routing core.**

The second is:

> **Every upstream attempt becomes observable and quota-accountable.**

The third is:

> **Routing can reject or prefer resources based on reliable quota/resource state before an upstream failure occurs.**

---

## 39. AI Agent Guardrails for Future Work

When an AI model is asked to implement a feature in this repository, it should ask internally:

### Does this belong in routing core?

Only if the behavior is provider-independent.

Provider-specific request/response/auth/quota behavior belongs in the driver or collector.

### Am I creating a new source of truth?

Avoid duplicate authoritative state. Durable configuration/ledger belongs in PostgreSQL. Runtime coordination belongs in Redis.

### Am I making an estimate look exact?

If yes, add provenance/confidence and rename fields so semantics are truthful.

### Am I introducing a new provider by special case?

If yes, stop and use a generic driver/template or a provider-specific driver outside core.

### Am I retrying without considering resource cost?

If yes, integrate retry budgets and usage uncertainty.

### Am I changing stream behavior?

Preserve the first-output invariant.

### Am I adding a later-scope feature because it is convenient?

Do not introduce a 1.1+ subsystem as an implicit dependency of 1.0.

### Am I changing an architecture decision?

Create/replace an ADR rather than silently diverging.

---

## 40. External Architectural References

The project design is informed by, but not coupled to:

- CLIProxyAPI: `https://github.com/router-for-me/CLIProxyAPI`
- ProxyPal: `https://github.com/heyhuynhgiabuu/proxypal`
- the existing `smart-router-ui` source baseline included with the project.

These repositories are references for integration patterns, provider handling, OAuth/account management and UI/telemetry concepts. Smart Router owns its own global resource model, usage ledger, quota engine and routing policy.

Provider-specific free quota, pricing and rate-limit values must **not** be permanently embedded in this README because they can change. Drivers and collectors should obtain runtime metadata where possible; otherwise configured/estimated values must include source and confidence.

---

## 41. Final Baseline

Implementation begins from **M0 + M1**.

The single most important architectural guardrail is:

> **Do not add another provider directly to `router.py` or equivalent routing-core code.**

A provider must enter the system through one of these paths:

```text
Generic Provider Driver
Provider Template
Provider-specific Driver/Collector
CLIProxy Bridge
Legacy Compatibility Adapter
```

Once Provider Registry, Usage Ledger and Quota Engine are reliable, Smart Scheduler can optimize free capacity using real telemetry rather than guesses.

That order is intentional and must be preserved.
