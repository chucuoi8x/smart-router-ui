# Security — Smart Router 1.0

## Threat model

| Asset | Threat vector | Mitigation |
|-------|---------------|------------|
| Provider credentials (api_key) | Exposure via API response, logs or database dump | Fernet encryption at rest; never returned through admin endpoints after initial creation |
| Project API keys | Unauthorized access to project-scoped operations | Encrypted hash stored; full key returned only at `POST /projects` creation; admin auth required for all endpoints |
| Admin tokens | Replay or theft via network eavesdropping | TLS in production deployment; bearer token transmitted over encrypted channel only |
| Configuration revisions | Accidental rollback of malicious config | Immutable revision store with explicit rollback audit event linking from_revision → to_revision |
| Audit log | Tampering or suppression | Append-only in-memory list during runtime; durable ledger in PostgreSQL under AC-04 provenance |
| Docker compose services | Container escape or uncontrolled data exposure | Read-only volumes where applicable, isolated networks, healthcheck-based lifecycle management |
| Legacy YAML migration | Code execution or arbitrary command injection | `yaml.safe_load`, manifest compiler rejects unsafe patterns (`__import__`, `eval`, `exec`, `subprocess`, etc.) |

## Authentication

All Control Plane endpoints under `/api/admin/v1/*` require `Authorization: Bearer $SMART_ROUTER_KEY`. Unauthenticated requests receive HTTP 401. The value is loaded from environment variable and never persisted.

## Encryption at rest

Credential values (provider api_key, project secret_key) are encrypted using Python `cryptography.fernet.Fernet`:

1. Environment variable `SMART_ROUTER_ENCRYPTION_KEY` must be set in production deployments.
2. If not configured, a deterministic dev fallback uses SHA-256 seed — this must NOT be used in production.
3. All encrypted values are stored as Base64 Fernet ciphertext in memory collections and database columns (`credential_encrypted`).
4. Serialization helpers (`_serialize_provider`, `_serialize_credential`) never expose the ciphertext field; they return `credential_present: true/false` instead.
5. The secret key from project creation is returned exactly once in the `POST /projects` response body. It is hashed via Fernet before storage and never re-returned.

## Secret scanning

CI and pre-commit verify no plaintext secrets exist in source files:

- Patterns: `sk-[A-Za-z0-9]{20,}` for provider keys, `ghp_[A-Za-z0-9]{20,}` for GitHub tokens.
- `.claude/` directory remains untracked and is excluded from git history.
- `.env.example` contains placeholder values only — never real keys.

Verification procedure:

```bash
grep -rE 'sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}' --include='*.py' --include='*.md' --include='*.yaml' . || echo "SCAN_CLEAN"
```

## Operational security

- Review audit log regularly: `GET /api/admin/v1/audit`
- Monitor alerts for critical severity: `GET /api/admin/v1/alerts?severity=critical`
- Rotate `SMART_ROUTER_KEY` when any admin credential may be compromised.
- Rotate `SMART_ROUTER_ENCRYPTION_KEY` to re-encrypt all stored credentials if detected in unauthorized access.
- Run backup restore tests monthly against staging environments.
