# Changelog

All notable changes to Smart Router are documented here.

## [1.0.0] — 2026-09-16

### Added

- Provider-agnostic `ProviderDriver` registry with OpenAI, Anthropic, Gemini and CLIProxy bridge support (AC-01, AC-02, AC-12).
- Provider onboarding APIs: templates, create, test connection, discover models, import models into routes (AC-01).
- Multi-credential lifecycle per provider: add/list/delete, encrypted at rest, redacted responses (AC-03, AC-14).
- Request, attempt and usage ledger with provenance, token accounting and cost fields (AC-04).
- Quota resources for request/token/credit/concurrency metrics, windows, shared groups and atomic reservation/reconciliation (AC-05, AC-06, AC-07).
- Capability, context, quota, auth, concurrency and budget eligibility filtering (AC-08).
- Scheduler presets: `auto-free`, `fast`, `coding`, `review`, `critical` with paid fallback ceilings (AC-09, AC-10).
- Streaming failover guard before first output (AC-11).
- Control Plane APIs: Overview, Providers, Models, Routes/Policies, Settings/Security, Usage/Ledger, Audit, Alerts, Revisions and rollback (AC-13).
- Project/API-key management with one-time secret display and encrypted key storage (AC-13, AC-14).
- Legacy `config.yaml` migration to validated immutable revisions (AC-16).
- Full Docker Compose stack: gateway, worker, web, PostgreSQL and Redis, with healthchecks and volumes (AC-15).
- Backup/restore documentation, operations runbook and SLO definitions.
- Runtime `/version` endpoints reporting `1.0.0`.

### Security

- Credentials encrypted using Fernet.
- Provider credentials, project key hashes and audit payloads never expose plaintext secrets.
- Admin endpoints require bearer authentication.
- Migration and revision activation produce redacted audit events.

### Verification

- Full automated suite: `432 passed, 6 skipped` at Step 130.
- Acceptance smoke tests cover provider onboarding, redaction, migration and Control Plane auth.
