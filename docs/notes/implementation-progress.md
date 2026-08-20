# Smart Router implementation progress handoff

Last updated: 2026-08-20

Purpose: this note helps a future human or AI agent continue the Smart Router migration without relying on chat history.

Progress tracking rule: every implementation step must be noted here as work proceeds, including date, files changed, verification commands, outcomes, and follow-up risks/TODOs. README.md section 31.4 records this as a standing project rule.

## Current repository state

- Working tree has many uncommitted changes.
- Branch is main.
- The original .env was backed up outside the repo and removed from the project directory.
- Backup path: g:/linhnh/claude/.env-backups/smart-router-ui.env.2026-08-19-104305.backup
- Current full test status: 42 passed, 2 failed, 3 warnings.
- The 2 failures are RED tests for Admin API endpoints that are not implemented yet.
- No admin API router files currently exist under apps/gateway/api.

## M0 progress

Implemented:
- X-Request-ID middleware in router.py.
- /health/live endpoint.
- /health/ready endpoint.
- Dockerfile and docker-compose.yml baseline.
- pyproject.toml with pytest configuration.
- Tests: tests/test_health_and_request_id.py and tests/test_streaming_failover.py.

Notes:
- Dockerfile has not been build-verified in this session.
- pytest warns about asyncio_mode, likely because pytest-asyncio is not installed in the current env.
- fastapi.testclient emits a Starlette deprecation warning about httpx.

## M1 progress

Implemented initial versions:
- apps/gateway/routing/models.py with ResourceRef, ResourceCandidate, Capability.
- apps/gateway/providers/base.py with ProviderDriver Protocol and DriverNotFoundError.
- apps/gateway/providers/registry.py with DriverRegistry.
- apps/gateway/config/snapshot.py with ConnectionConfig, RouteConfig, RuntimeConfigSnapshot.
- apps/gateway/config/compiler.py with LegacyConfigCompiler.compile_file.
- apps/gateway/routing/engine.py with initial RouterEngine and InMemoryCircuitRepository.
- apps/gateway/providers/generic_anthropic.py with initial GenericAnthropicDriver.
- aibox_catalog.py moved to apps/worker/collectors/aibox_catalog.py.
- tests/test_catalog.py imports from apps.worker.collectors.aibox_catalog.

Tests added:
- tests/unit/test_domain_models.py
- tests/unit/test_router_engine.py
- tests/unit/test_anthropic_driver.py

Important cautions:
- router.py still uses the legacy SmartRouter for live endpoints.
- RouterEngine exists but is not wired into FastAPI request handling yet.
- router.py still references build_records, select_routes, and state_from_records in sync_catalog, but the import was replaced with a comment. Runtime catalog sync will break until this import or relocation is fixed.
- Legacy router.py still contains provider-specific cooldown logic for aibox. Final architecture must move this out of routing core.

## M1.5 database baseline status

Partially implemented or attempted:
- apps/gateway/db/session.py exists.
- alembic.ini exists.
- migrations/env.py exists.
- Verify apps/gateway/db/models.py before relying on it because an earlier batch command partially failed.

Cautions:
- Alembic revision generation failed earlier due to wrong executable path.
- No migration revision has been verified.
- Verify SQLAlchemy and Alembic dependencies are reproducible in pyproject.toml or requirements.txt.

## M2.1 to M2.3 progress

Implemented initial versions:
- apps/gateway/providers/generic_openai.py
- apps/gateway/providers/generic_gemini.py
- apps/gateway/providers/cliproxy_bridge.py

Tests added:
- tests/unit/test_generic_drivers.py

Notes:
- These drivers currently implement usage parsing and capability metadata only.
- execute, execute_stream, and discover_models are stubs.

## M2.4 to M2.7 progress

Implemented initial versions:
- templates/providers/openai.yaml
- templates/providers/anthropic.yaml
- apps/gateway/config/templates.py
- apps/gateway/config/manifest.py
- apps/gateway/config/revision.py

Tests added:
- tests/unit/test_templates_and_revisions.py
- tests/unit/test_admin_api.py

Current status:
- test_templates_and_revisions.py passes.
- test_admin_api.py fails with 404 because admin routes are not implemented or mounted.

Next RED-GREEN target:
- Implement GET /api/admin/v1/templates.
- Implement POST /api/admin/v1/providers.
- Implement GET /api/admin/v1/revisions/active.
- Implement POST /api/admin/v1/revisions.
- Implement POST /api/admin/v1/revisions/{revision_id}/activate.
- Mount the admin router in router.py with prefix /api/admin/v1.
- Use simple admin auth for tests: Authorization header equals Bearer test-admin-key.

## Known failing tests

Run:
cd g:/linhnh/claude/smart-router-ui
g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q

Expected current result:
42 passed, 2 failed, 3 warnings.

The failures are Admin API 404s, not broad regressions.

## Recommended immediate next steps

1. Implement and mount admin API routes to satisfy tests/unit/test_admin_api.py.
2. Run unittest for tests.unit.test_admin_api, then run full pytest.
3. Fix router.py import after aibox_catalog.py move or remove catalog sync from SmartRouter fully.
4. Verify apps/gateway/db/models.py and Alembic files.
5. Commit in logical chunks once tests pass and no secrets are staged.

## Architecture guardrails

- Do not add provider-name conditionals to routing core.
- Provider-specific behavior belongs in drivers, templates, collectors, bridges, or compatibility modules.
- Do not treat estimated usage or quota as exact provider truth.
- Do not fail over streaming after first upstream output has been sent.
- Do not store secrets in plaintext DB fields, logs, audit diffs, or API responses.
- Configuration changes should become versioned revisions and activate atomically.

## File map

New and changed areas include:
- apps/gateway/config
- apps/gateway/providers
- apps/gateway/routing
- apps/gateway/db
- apps/worker/collectors/aibox_catalog.py
- templates/providers
- tests/unit
- Dockerfile, docker-compose.yml, pyproject.toml, alembic.ini, migrations/env.py

## Commit status

No successful commit has been made during this handoff segment.
Before committing, run git status --short, git diff --check, and full pytest.

## 2026-08-20 continuation log

### Step 1 - Progress tracking rule documented

Changed:
- Added README.md section 31.4 to require recording every implementation step in this file.
- Added the same progress tracking rule near the top of this handoff note.

Verification:
- Documentation-only step; no tests required before moving to the Admin API RED check.

Outcome:
- Progress tracking requirement is now recorded in both README.md and this implementation progress note.

### Step 2 - Admin API RED state confirmed

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_admin_api.py -q`.

Outcome:
- Confirmed the expected RED state: 2 failures in `tests/unit/test_admin_api.py`.
- Both failures are 404s for missing `/api/admin/v1` routes.
- Warnings remain the existing pytest `asyncio_mode` warning and FastAPI/Starlette TestClient deprecation warning.

### Step 3 - Minimal Admin API slice implemented

Changed:
- Added `apps/gateway/api/__init__.py`.
- Added `apps/gateway/api/admin.py` with a FastAPI `APIRouter` for `/templates`, `/providers`, `/revisions/active`, `/revisions`, and `/revisions/{revision_id}/activate`.
- Mounted the admin router in `router.py` with prefix `/api/admin/v1`.
- Added a regression assertion in `tests/unit/test_admin_api.py` to ensure provider creation responses do not echo `api_key`.

Implementation notes:
- Admin auth is deliberately separate from data-plane auth and currently accepts only `Authorization: Bearer test-admin-key` for this test slice.
- Provider templates reuse `ProviderTemplateRegistry`.
- Revision lifecycle reuses `ConfigRevisionManager` with a lazily created baseline active revision.
- Provider connection state is in-memory only; no DB/Alembic changes were made.
- Provider secrets are not stored or returned; only `credential_present` is recorded in memory for this temporary slice.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_admin_api.py -q`.

Outcome:
- Admin API focused tests pass: 2 passed, 4 warnings.
- Warnings include existing pytest/FastAPI warnings plus `datetime.utcnow()` deprecation from `apps/gateway/config/revision.py`.

### Step 4 - Full verification completed

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.
- Ran `git diff --check`.
- Ran `git status --short --untracked-files=all`.

Outcome:
- Full pytest passes: 44 passed, 5 warnings.
- `git diff --check` reported no whitespace errors; it did report the existing line-ending warning that `README.md` LF will be replaced by CRLF when Git touches it.
- `git status --short --untracked-files=all` still shows the broader uncommitted migration work from this handoff plus the new Admin API files and documentation edits.

Follow-up notes:
- The `datetime.utcnow()` deprecation in `apps/gateway/config/revision.py` remains a cleanup candidate for a later small slice.
- Dockerfile, DB/Alembic, and live `RouterEngine` wiring remain out of scope for this Admin API slice.

### Step 5 - Final completion verification

Verification:
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q` before reporting completion.
- Re-ran `git diff --check`.
- Re-ran `git status --short --untracked-files=all`.

Outcome:
- Final full pytest result: 44 passed, 5 warnings.
- Final `git diff --check` result: no whitespace errors; README.md still reports the Git line-ending warning (`LF will be replaced by CRLF`).
- Workspace is a normal checkout on `main` (`git_dir=.git`, `git_common=.git`), not a linked worktree.
- No commit or merge was made.

### Step 6 - Local runtime smoke test

Verification:
- Started the FastAPI app with `uvicorn router:app --host 127.0.0.1 --port 8321` using local test environment variables.
- Called `GET http://127.0.0.1:8321/health/live`.
- Called `GET http://127.0.0.1:8321/health/ready`.
- Called `GET http://127.0.0.1:8321/api/admin/v1/templates` with `Authorization: Bearer test-admin-key`.
- Called `GET http://127.0.0.1:8321/v1/models` with `x-api-key: test-router-key`.
- Called `POST http://127.0.0.1:8321/api/admin/v1/providers` with `Authorization: Bearer test-admin-key`.
- Re-checked `GET http://127.0.0.1:8321/health/live` before reporting runtime status.

Outcome:
- Local API server is running on `127.0.0.1:8321`.
- `/health/live` returns `{"status":"ok","service":"smart-router"}`.
- `/health/ready` returns `{"status":"ok","service":"smart-router","upstream_count":3}`.
- `/api/admin/v1/templates` returns the OpenAI and Anthropic templates.
- `/v1/models` returns the configured logical router models.
- `POST /api/admin/v1/providers` succeeds and does not return the submitted `api_key`.

Follow-up notes:
- This confirms the local Python/FastAPI app path runs.
- Docker Compose runtime is still not verified; Dockerfile is known to be stale because it references the moved root `aibox_catalog.py` and does not copy the new `apps/` tree.

### Step 7 - Git save branch prepared

Changed:
- Created branch `feature/admin-api-baseline` from `main` before committing/pushing the current worktree state.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q` before staging.
- Ran `git diff --check` before staging.

Outcome:
- Pre-commit full pytest result: 44 passed, 5 warnings.
- `git diff --check` reported no whitespace errors; README.md still reports the Git line-ending warning (`LF will be replaced by CRLF`).

### Step 8 - Current state committed and pushed

Changed:
- Stripped trailing whitespace from staged text files before commit because `git diff --cached --check` caught whitespace issues in the handoff batch.
- Committed the current migration baseline on branch `feature/admin-api-baseline`.
- Pushed the branch to `origin/feature/admin-api-baseline`.

Verification:
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q` after whitespace cleanup.
- Re-ran `git diff --cached --check` after restaging.
- Scanned staged diff for secret-like terms before commit; matches were placeholder/test values, environment variable names, token field names, and documentation references, not real secrets.

Outcome:
- Commit pushed: `12edc00 feat: add smart router admin baseline`.
- Remote branch: `origin/feature/admin-api-baseline`.
- GitHub PR URL suggested by remote: `https://github.com/chucuoi8x/smart-router-ui/pull/new/feature/admin-api-baseline`.

### Step 9 - Docker runtime packaging guard added

Changed:
- Added `tests/unit/test_docker_packaging.py` to assert the gateway Docker image includes the new runtime package directories and no longer references the moved root `aibox_catalog.py`.
- Updated `Dockerfile` to copy `router.py`, `config.yaml`, `apps/`, `templates/`, `migrations/`, and `alembic.ini` into the image.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_docker_packaging.py -q` before the Dockerfile fix; it failed because `COPY apps ./apps` was missing and `aibox_catalog.py` was still referenced.
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_docker_packaging.py -q` after the fix.
- Attempted `docker compose build gateway`.

Outcome:
- Docker packaging focused test now passes: 1 passed, 1 warning.
- Docker build could not be verified in this environment because Docker CLI is not installed or not on PATH (`docker` is not recognized).

Follow-up notes:
- Run `docker compose build gateway` and `docker compose up gateway` on a machine with Docker installed before treating Docker runtime as fully verified.

### Step 10 - Docker packaging slice verified

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q` after the Dockerfile/test changes.
- Ran `git diff --check`.
- Ran `git status --short --untracked-files=all`.

Outcome:
- Full pytest result after this slice: 45 passed, 5 warnings.
- `git diff --check` reported no whitespace errors; Dockerfile and this progress note report Git line-ending warnings (`LF will be replaced by CRLF`).
- Pending changes for this slice before commit: `Dockerfile`, `docs/notes/implementation-progress.md`, and `tests/unit/test_docker_packaging.py`.
- Commit pushed: `5c5751b test: guard Docker gateway packaging`.

### Step 11 - Timezone-aware revision timestamps

Changed:
- Updated `apps/gateway/config/revision.py` to use `datetime.now(UTC)` instead of deprecated `datetime.utcnow()`.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_templates_and_revisions.py tests/unit/test_admin_api.py -q`.
- Searched Python files for `utcnow(`.

Outcome:
- Targeted revision/Admin tests pass: 5 passed, 2 warnings.
- No remaining `utcnow(` matches in Python files.
- The previous `datetime.utcnow()` deprecation warning is removed from the targeted test run.

Final verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.
- Ran `git diff --check`.
- Ran `git status --short --untracked-files=all`.

Final outcome:
- Full pytest result after timestamp fix: 45 passed, 2 warnings.
- Remaining warnings are the existing pytest `asyncio_mode` config warning and FastAPI/Starlette TestClient deprecation warning.
- `git diff --check` reported no whitespace errors; this progress note reports the Git line-ending warning (`LF will be replaced by CRLF`).
- Commit pushed: `014d05f fix: use timezone-aware revision timestamps`.

### Step 12 - Legacy catalog import compatibility restored

Changed:
- Added `tests/unit/test_catalog_import_compat.py` to assert `router.py` exposes `build_records`, `select_routes`, and `state_from_records`.
- Restored the explicit import in `router.py` from `apps.worker.collectors.aibox_catalog`.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_catalog_import_compat.py -q` before the fix; it failed with `AttributeError: module 'router' has no attribute 'build_records'`.
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_catalog_import_compat.py -q` after the fix; it passed.
- Ran full pytest `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.
- Ran `git diff --check`.
- Ran `git status --short --untracked-files=all`.

Outcome:
- Full pytest result: 46 passed, 2 warnings.
- `git diff --check` produced zero warnings or errors.
- Legacy `sync_catalog()` path no longer risks `NameError` at runtime when calling moved catalog helpers.

### Step 13 - Database baseline and Alembic migration added

Changed:
- Added `tests/unit/test_db_models.py` to define the expected SQLAlchemy baseline model behavior and Alembic revision importability.
- Added `apps/gateway/db/models.py` with SQLAlchemy 2.0 declarative models for `provider_connections`, `config_revisions`, and `usage_ledger`.
- Added `migrations/versions/001_initial_baseline.py` with the initial Alembic migration for the three baseline tables and indexes.
- Updated `migrations/env.py` to use Alembic's async migration pattern with `async_engine_from_config` and `connection.run_sync()` for the existing `postgresql+asyncpg` URL.
- Updated `alembic.ini` with standard logging sections required by `fileConfig`.
- Added `sqlalchemy`, `alembic`, and `asyncpg` to `pyproject.toml` and `requirements.txt` so DB/Alembic dependencies are reproducible.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_db_models.py -q` before the model implementation; it failed with `ModuleNotFoundError: No module named 'apps.gateway.db.models'`.
- Installed DB dependencies into the project `.venv` using `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pip install sqlalchemy alembic asyncpg pytest-asyncio` after the first RED run exposed missing local SQLAlchemy packages.
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_db_models.py -q` after implementation.
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m alembic upgrade head --sql` to verify offline SQL generation without requiring a live database.
- Ran full pytest `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.

Outcome:
- DB baseline focused tests pass: 5 passed.
- Alembic offline SQL generation succeeds and emits `CREATE TABLE` statements for `provider_connections`, `config_revisions`, and `usage_ledger`.
- Full pytest result after this slice: 51 passed, 1 warning.
- Remaining warning is the existing FastAPI/Starlette TestClient deprecation warning.

### Step 14 - Catalog and DB baseline commits pushed

Changed:
- Committed the legacy catalog compatibility slice separately as `fdb8c21 test: restore catalog import compatibility`.
- Committed the DB/Alembic baseline slice separately as `daaf684 feat: add database baseline models`.
- Pushed `feature/admin-api-baseline` to `origin/feature/admin-api-baseline`.

Verification:
- Ran `git diff --cached --check` before the catalog compatibility commit.
- Inspected the staged catalog compatibility diff for secret-like terms before commit.
- Ran `git diff --cached --check` before the DB/Alembic baseline commit.
- Inspected the staged DB/Alembic baseline diff for secret-like terms before commit; matches were model field names, usage token fields, and test placeholder values only.
- Ran `git push origin feature/admin-api-baseline`.

Outcome:
- Remote branch advanced from `014d05f` to `216ebea` after the follow-up progress-note commit.
- Two logical implementation commits and one documentation tracking commit are now available on the remote feature branch.

### Step 15 - First M3 usage ledger domain slice

Changed:
- Added `tests/unit/test_usage_ledger.py` covering usage event normalization, request-level totals across multiple attempts, and privacy filtering for request metadata.
- Added `apps/gateway/usage/__init__.py` package marker.
- Added `apps/gateway/usage/ledger.py` with `UsageEvent`, `RequestRecord`, `AttemptRecord`, and `InMemoryUsageLedger`.

Implementation notes:
- This is a domain/service slice only; it is not yet wired into live `router.py`, DB persistence, or quota admission control.
- Request and attempt records are intentionally separate so a single client request can retain multiple upstream attempts.
- Request-level totals include all usage events for the request, including failed/retried attempts that may have consumed capacity.
- `record_request()` filters private payload/secret metadata keys such as raw prompts, raw responses, authorization headers, cookies, and provider secrets.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_usage_ledger.py -q` before implementation; it failed with `ModuleNotFoundError: No module named 'apps.gateway.usage'`.
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_usage_ledger.py -q` after implementation.
- Ran full pytest `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.

Outcome:
- Usage ledger focused tests pass: 3 passed.
- Full pytest result after this slice: 54 passed, 1 warning.
- Remaining warning is the existing FastAPI/Starlette TestClient deprecation warning.

### Step 16 - Usage ledger slice pushed

Changed:
- Committed the first M3 usage ledger domain slice as `b570f3c feat: add usage ledger domain slice`.
- Pushed `feature/admin-api-baseline` to `origin/feature/admin-api-baseline`.

Verification:
- Ran `git diff --cached --check` before commit.
- Inspected staged diff for secret-like terms before commit; matches were privacy filter keys, token usage field names, and placeholder values in tests used to verify secret filtering.
- Ran `git push origin feature/admin-api-baseline`.

Outcome:
- Remote branch advanced from `216ebea` to `b570f3c`.
- First M3 usage ledger domain slice is now available on the remote feature branch.

### Step 17 - UsageEvent persistence mapping and provenance migration

Changed:
- Extended `tests/unit/test_usage_ledger.py` with a RED test for mapping `UsageEvent` to the SQLAlchemy `UsageLedger` model while preserving attempt/provenance/accounting fields.
- Extended `apps/gateway/usage/ledger.py` with `UsageEvent.to_db_model()`.
- Extended `apps/gateway/db/models.py` `UsageLedger` columns with `attempt_id`, `credential_id`, cached/cache-write/reasoning/total token fields, native metric fields, currency, source, confidence, and estimated flag.
- Added `migrations/versions/002_usage_ledger_provenance.py` to migrate existing `usage_ledger` tables from the initial baseline to the richer provenance schema.
- Extended `tests/unit/test_db_models.py` to assert migration revision `002_usage_ledger_provenance` is importable and chained after `001_initial_baseline`.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_usage_ledger.py -q` before implementation; it failed with `AttributeError: 'UsageEvent' object has no attribute 'to_db_model'`.
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_usage_ledger.py tests/unit/test_db_models.py -q` after implementation.
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m alembic upgrade head --sql` to verify the `001 -> 002` offline migration chain.
- Ran full pytest `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.

Outcome:
- Focused usage/DB model tests pass: 10 passed.
- Alembic offline SQL generation succeeds and emits upgrade SQL for `002_usage_ledger_provenance`.
- Full pytest result after this slice: 56 passed, 1 warning.
- Remaining warning is the existing FastAPI/Starlette TestClient deprecation warning.

### Step 18 - UsageEvent persistence slice pushed

Changed:
- Committed the UsageEvent persistence/provenance slice as `80dfcc8 feat: persist usage event provenance`.
- Pushed `feature/admin-api-baseline` to `origin/feature/admin-api-baseline`.

Verification:
- Ran `git diff --cached --check` before commit.
- Inspected staged diff for secret-like terms before commit; matches were usage token field names and existing privacy-filter assertions only.
- Ran `git push origin feature/admin-api-baseline`.

Outcome:
- Remote branch advanced from `c3fd28e` to `80dfcc8`.
- UsageEvent-to-DB persistence mapping and migration `002_usage_ledger_provenance` are now available on the remote feature branch.

### Step 19 - Async UsageLedger repository boundary

Changed:
- Extended `tests/unit/test_usage_ledger.py` with RED tests for an async repository that stores a `UsageEvent` through a SQLAlchemy-like session.
- Added `UsageLedgerRepository` in `apps/gateway/usage/ledger.py`.

Implementation notes:
- The repository accepts an injected async session boundary instead of importing the global session, which keeps it testable and usable with FastAPI dependency injection later.
- `record_usage()` maps `UsageEvent` to `UsageLedger`, calls `session.add()`, always flushes, and commits only when explicitly requested.
- This slice still does not wire usage persistence into `router.py`; that remains a later integration slice.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_usage_ledger.py -q` before implementation; it failed with `ImportError: cannot import name 'UsageLedgerRepository'`.
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_usage_ledger.py -q` after implementation.
- Ran full pytest `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.

Outcome:
- Usage ledger focused tests pass: 6 passed.
- Full pytest result after this slice: 58 passed, 1 warning.
- Remaining warning is the existing FastAPI/Starlette TestClient deprecation warning.

### Step 20 - UsageLedger repository slice pushed

Changed:
- Committed the async UsageLedger repository slice as `4b23f96 feat: add usage ledger repository`.
- Pushed `feature/admin-api-baseline` to `origin/feature/admin-api-baseline`.

Verification:
- Ran `git diff --cached --check` before commit.
- Inspected staged diff for secret-like terms before commit; matches were usage token field names only.
- Ran `git push origin feature/admin-api-baseline`.

Outcome:
- Remote branch advanced from `ebaea1a` to `4b23f96`.
- The M3 usage ledger now has a tested async repository boundary for future FastAPI/DB wiring.

### Step 21 - Request and attempt ledger persistence

Changed:
- Extended `tests/unit/test_usage_ledger.py` with RED tests for mapping `RequestRecord` and `AttemptRecord` to SQLAlchemy models and persisting them through `UsageLedgerRepository`.
- Extended `tests/unit/test_db_models.py` to include `request_ledger` and `attempt_ledger` metadata plus migration revision `003_request_attempt_ledger` importability.
- Added `RequestLedger` and `AttemptLedger` SQLAlchemy models in `apps/gateway/db/models.py`.
- Added `RequestRecord.to_db_model()` and `AttemptRecord.to_db_model()` in `apps/gateway/usage/ledger.py`.
- Added `UsageLedgerRepository.record_request()` and `UsageLedgerRepository.record_attempt()`.
- Added `migrations/versions/003_request_attempt_ledger.py` creating the request and attempt ledger tables and indexes.

Implementation notes:
- This completes the first persistence shape for README M3's Request / Attempt / UsageEvent ledger requirement.
- The request table stores sanitized metadata from `RequestRecord`; raw prompt/response and secret-like fields remain filtered by earlier domain logic.
- This slice still does not wire live `router.py` request handling to the repository.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_usage_ledger.py tests/unit/test_db_models.py -q` before implementation; it failed because `RequestLedger` and `AttemptLedger` did not exist.
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_usage_ledger.py tests/unit/test_db_models.py -q` after implementation.
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m alembic upgrade head --sql` to verify the `001 -> 002 -> 003` offline migration chain.
- Ran full pytest `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.

Outcome:
- Focused usage/DB tests pass: 15 passed.
- Alembic offline SQL generation succeeds and emits `CREATE TABLE request_ledger` and `CREATE TABLE attempt_ledger` for migration `003_request_attempt_ledger`.
- Full pytest result after this slice: 61 passed, 1 warning.
- Remaining warning is the existing FastAPI/Starlette TestClient deprecation warning.

### Step 22 - Request and attempt ledger slice pushed

Changed:
- Committed the request/attempt ledger persistence slice as `45c679a feat: persist request and attempt ledgers`.
- Pushed `feature/admin-api-baseline` to `origin/feature/admin-api-baseline`.

Verification:
- Ran `git diff --cached --check` before commit.
- Inspected staged diff for secret-like terms before commit; matches were documentation about secret filtering and existing test assertions only.
- Ran `git push origin feature/admin-api-baseline`.

Outcome:
- Remote branch advanced from `044b87a` to `45c679a`.
- Request, attempt, and usage event ledger persistence scaffolding is now available on the remote feature branch.

### Step 23 - Quota resource domain and in-memory reservation

Changed:
- Added `tests/unit/test_quota_reservations.py` with RED tests for quota resource snapshots, hard-limit rejection, release behavior, and concurrent reservation safety.
- Added `apps/gateway/quota/__init__.py` package marker.
- Added `apps/gateway/quota/reservations.py` with `QuotaResource`, `ReservationResult`, and `InMemoryQuotaReservations`.

Implementation notes:
- This is an in-memory Resource Plane domain slice only; it is not wired into live `router.py`, Redis, DB persistence, or usage-ledger accounting yet.
- `InMemoryQuotaReservations.reserve()` uses a process-local `threading.Lock` so concurrent reservations in this process cannot oversubscribe a hard limit.
- Reservation IDs are idempotent: repeating the same reservation ID returns the original result instead of consuming additional capacity.
- Rejected reservations are recorded as rejected results but do not consume capacity; accepted reservations can be released to return capacity.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py -q` before implementation; it failed with `ModuleNotFoundError: No module named 'apps.gateway.quota'`.
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py -q` after implementation.
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py tests/unit/test_usage_ledger.py tests/unit/test_db_models.py -q`.
- Ran full pytest `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.

Outcome:
- Quota focused tests pass: 4 passed.
- Focused M3 unit tests pass: 19 passed.
- Full pytest result after this slice: 65 passed, 1 warning.
- Remaining warning is the existing FastAPI/Starlette TestClient deprecation warning.

### Step 24 - Quota reservation slice pushed

Changed:
- Committed the quota resource/in-memory reservation slice as `12b4e1a feat: add quota reservation domain`.
- Pushed `feature/admin-api-baseline` to `origin/feature/admin-api-baseline`.

Verification:
- Ran `git diff --cached --check` before commit.
- Inspected staged diff for secret-like terms before commit; no secret-like terms were present.
- Ran `git push origin feature/admin-api-baseline`.

Outcome:
- Remote branch advanced from `4de1a09` to `12b4e1a`.
- The M3 Resource Plane now has a tested in-memory quota reservation domain for future router/Redis integration.

### Step 25 - Atomic multi-constraint reservation

Changed:
- Extended `tests/unit/test_quota_reservations.py` with RED tests for all-or-nothing multi-resource reservation and releasing multi-resource reservations.
- Extended `apps/gateway/quota/reservations.py` with `QuotaReservationRequest`, `ReservationBatchResult`, and `InMemoryQuotaReservations.reserve_many()`.

Implementation notes:
- This matches README 15.8's atomic reservation semantics in the in-memory prototype: a multi-constraint reservation either reserves every requested resource or reserves none.
- If any hard constraint lacks capacity, `reserve_many()` returns a rejected batch result with `reason="quota_exceeded"` and `rejected_resource_id` identifying the failing resource.
- Repeating a reservation ID remains idempotent and returns the stored single-resource or batch result.
- `release()` now handles both single-resource and multi-resource accepted reservations.
- This remains process-local only; Redis/Lua distributed atomicity remains a later Resource Plane integration slice.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py -q` before implementation; the two new tests failed with `ImportError: cannot import name 'QuotaReservationRequest'`.
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py -q` after implementation.
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py tests/unit/test_usage_ledger.py tests/unit/test_db_models.py -q`.
- Ran full pytest `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.

Outcome:
- Quota focused tests pass: 6 passed.
- Focused M3 unit tests pass: 21 passed.
- Full pytest result after this slice: 67 passed, 1 warning.
- Remaining warning is the existing FastAPI/Starlette TestClient deprecation warning.

### Step 26 - Atomic quota batch reservation slice pushed

Changed:
- Committed the atomic multi-constraint reservation slice as `09fb915 feat: add atomic quota batch reservation`.
- Pushed `feature/admin-api-baseline` to `origin/feature/admin-api-baseline`.

Verification:
- Ran `git diff --cached --check` before commit.
- Inspected staged diff for secret-like terms before commit; no secret-like terms were present.
- Ran `git push origin feature/admin-api-baseline`.

Outcome:
- Remote branch advanced from `1f47bc6` to `09fb915`.
- The in-memory quota prototype now supports README 15.8-style all-or-nothing reservation across multiple hard constraints.

### Step 27 - Quota reservation reconciliation

Changed:
- Extended `tests/unit/test_quota_reservations.py` with RED tests for reconciliation releasing unused reservations, recording overshoot, and reconciling multi-resource reservations.
- Extended `apps/gateway/quota/reservations.py` with `ReconciliationResult` and `InMemoryQuotaReservations.reconcile()`.

Implementation notes:
- This follows README 15.9's reconciliation model in the in-memory prototype: actual usage replaces the reserved estimate after upstream completion.
- If actual usage is lower than the reservation, unused capacity is released from the in-memory counter.
- If actual usage is higher than the reservation, overshoot is recorded and the counter is increased to the actual amount.
- Reconciliation is idempotent by `reservation_id`; a repeated call returns the original `ReconciliationResult` and does not mutate counters again.
- Once a reservation has been reconciled, `release()` no longer returns capacity for that same reservation ID.
- This remains process-local only; durable ledger integration and Redis distributed reconciliation are later slices.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py -q` before implementation; the three new tests failed with `AttributeError: 'InMemoryQuotaReservations' object has no attribute 'reconcile'`.
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py -q` after implementation.
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py tests/unit/test_usage_ledger.py tests/unit/test_db_models.py -q`.
- Ran full pytest `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.

Outcome:
- Quota focused tests pass: 9 passed.
- Focused M3 unit tests pass: 24 passed.
- Full pytest result after this slice: 70 passed, 1 warning.
- Remaining warning is the existing FastAPI/Starlette TestClient deprecation warning.

### Step 28 - Quota reconciliation slice pushed

Changed:
- Committed the quota reconciliation slice as `c2d9e9b feat: reconcile quota reservations`.
- Pushed `feature/admin-api-baseline` to `origin/feature/admin-api-baseline`.

Verification:
- Ran `git diff --cached --check` before commit.
- Inspected staged diff for secret-like terms before commit; no secret-like terms were present.
- Ran `git push origin feature/admin-api-baseline`.

Outcome:
- Remote branch advanced from `2464622` to `c2d9e9b`.
- The in-memory quota prototype now supports README 15.9-style reservation reconciliation with unused release, overshoot accounting, and idempotent repeated reconciliation.

### Step 29 - Quota safety buffer and risk-buffered reservation

Changed:
- Extended `tests/unit/test_quota_reservations.py` with RED tests for resource safety buffers, per-reservation risk buffers, and per-constraint risk buffers in multi-resource reservations.
- Extended `apps/gateway/quota/reservations.py` so `QuotaResource` exposes `safety_buffer` and `effective_remaining`, and `QuotaReservationRequest` supports `risk_buffer`.
- Updated `InMemoryQuotaReservations.reserve()` and `reserve_many()` to check README 15.8-style `required = expected_consumption + risk_buffer` against effective capacity.

Implementation notes:
- `QuotaResource.remaining` remains the raw `limit - used` value, while `effective_remaining` subtracts `safety_buffer` for admission decisions.
- Single-resource reservations now store the required amount (`amount + risk_buffer`) so reconciliation can release unused risk buffer once actual usage is known.
- Multi-resource reservations apply each request's risk buffer independently and still remain all-or-nothing.
- This remains an in-memory prototype; safety tuning, estimator feedback, Redis/Lua atomicity, and router admission wiring remain later slices.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py -q` before implementation; the three new tests failed with unexpected `safety_buffer`/`risk_buffer` keyword arguments.
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py -q` after implementation.
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py tests/unit/test_usage_ledger.py tests/unit/test_db_models.py -q`.
- Ran full pytest `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.

Outcome:
- Quota focused tests pass: 12 passed.
- Focused M3 unit tests pass: 27 passed.
- Full pytest result after this slice: 73 passed, 1 warning.
- Remaining warning is the existing FastAPI/Starlette TestClient deprecation warning.

### Step 30 - Quota safety/risk buffer slice pushed

Changed:
- Committed the quota safety/risk buffer slice as `c0ba609 feat: add quota safety and risk buffers`.
- Pushed `feature/admin-api-baseline` to `origin/feature/admin-api-baseline`.

Verification:
- Ran `git diff --cached --check` before commit.
- Inspected staged diff for secret-like terms before commit; the only match was the progress note heading text for the safety/risk-buffer slice, not a secret.
- Ran `git push origin feature/admin-api-baseline`.

Outcome:
- Remote branch advanced from `cca05ab` to `c0ba609`.
- The in-memory quota prototype now models README 15.8's `effective_remaining` and `required = expected_consumption + risk_buffer` formulas.

### Step 31 - Hard and soft quota admission checks

Changed:
- Extended `tests/unit/test_quota_reservations.py` with RED tests for non-mutating admission checks that distinguish hard quota failures from soft quota pressure.
- Extended `apps/gateway/quota/reservations.py` with `QuotaResource.hard_limit`, `QuotaAdmissionResult`, and `InMemoryQuotaReservations.check_many()`.

Implementation notes:
- This follows README 15.5's eligibility rule: all hard constraints must pass, while soft constraints contribute pressure/penalty to scheduling.
- `check_many()` is intentionally non-mutating; it reports hard shortfalls, soft pressure, and effective remaining values without consuming capacity.
- Soft constraints with shortfall keep `accepted=True` so later scheduler/scoring logic can decide how strongly to penalize the candidate.
- Hard constraints with shortfall set `accepted=False` and report the shortfall by resource ID.
- This slice does not wire admission checks into `RouterEngine` or the live FastAPI data plane yet.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py -q` before implementation; the new tests failed with missing `hard_limit` and missing `check_many()`.
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py -q` after implementation.
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py tests/unit/test_usage_ledger.py tests/unit/test_db_models.py -q`.
- Ran full pytest `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.

Outcome:
- Quota focused tests pass: 14 passed.
- Focused M3 unit tests pass: 29 passed.
- Full pytest result after this slice: 75 passed, 1 warning.
- Remaining warning is the existing FastAPI/Starlette TestClient deprecation warning.

### Step 32 - Hard/soft quota admission slice pushed

Changed:
- Committed the hard/soft quota admission slice as `df5b2f5 thêm kiểm tra quota cứng và mềm`.
- Pushed `feature/admin-api-baseline` to `origin/feature/admin-api-baseline`.

Verification:
- Ran `git diff --cached --check` before commit.
- Inspected staged diff for secret-like terms before commit; no secret-like terms were present.
- Ran `git push origin feature/admin-api-baseline`.

Outcome:
- Remote branch advanced from `1c89be0` to `df5b2f5`.
- Starting with this slice, Git commit messages are written in Vietnamese per the user's instruction.
- The quota domain now has a non-mutating admission check that separates hard quota failures from soft quota pressure for future scheduler integration.

### Step 33 - Quota observation provenance updates

Changed:
- Extended `tests/unit/test_quota_reservations.py` with RED tests for default quota provenance and applying quota observations with source/confidence metadata.
- Extended `apps/gateway/quota/reservations.py` with `QuotaResource.source`, `QuotaResource.confidence`, `QuotaObservation`, and `InMemoryQuotaReservations.apply_observation()`.

Implementation notes:
- This follows README 15.6's guidance that quota values must carry source and confidence rather than pretending all values are exact provider truth.
- New `QuotaResource` instances default to `source="configured"` and `confidence="high"`.
- `apply_observation()` updates `limit`, `used`, `source`, and `confidence` while preserving resource identity fields such as scope, metric, and window.
- Observations may also update `safety_buffer` and `hard_limit` when those fields are known from the observation source.
- This remains in-memory only; provider collectors, response-header parsing, and DB persistence remain later slices.

Verification:
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py -q` before implementation; the new tests failed with missing `QuotaObservation` and missing `QuotaResource.source`.
- Re-ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py -q` after implementation.
- Ran `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest tests/unit/test_quota_reservations.py tests/unit/test_usage_ledger.py tests/unit/test_db_models.py -q`.
- Ran full pytest `g:/linhnh/claude/smart-router-ui/.venv/Scripts/python.exe -m pytest -q`.

Outcome:
- Quota focused tests pass: 16 passed.
- Focused M3 unit tests pass: 31 passed.
- Full pytest result after this slice: 77 passed, 1 warning.
- Remaining warning is the existing FastAPI/Starlette TestClient deprecation warning.
