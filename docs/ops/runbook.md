# Runbook — Smart Router 1.0

Vận hành self-hosted đầy đủ cho bản 1.0 (gateway + worker + web + postgres + redis).

## 1. Compose stack

```bash
cp .env.example .env
# điền SMART_ROUTER_KEY, SMART_ROUTER_ENCRYPTION_KEY, POSTGRES_PASSWORD
docker compose up -d --build
docker compose ps
docker compose logs -f gateway worker
```

Services: `gateway:8320`, `postgres:5432`, `redis:6379`, `worker`, `web:80` (nginx proxy `/api` → gateway). Health:
`GET /health`, `GET /api/admin/v1/overview` (requires `Authorization: Bearer $SMART_ROUTER_KEY`).

Env chính: `DATABASE_URL`, `REDIS_URL`, `SMART_ROUTER_ENCRYPTION_KEY` (Fernet), `SMART_ROUTER_KEY` (admin auth), `WORKER_POLL_SECONDS`.

## 2. Migration legacy `config.yaml` → revision

Control Plane lưu immutable revisions, không sửa file thủ công.

```bash
# qua API (không lộ secret)
curl -X POST http://localhost:8320/api/admin/v1/migration/yaml \
  -H "Authorization: Bearer $SMART_ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"yaml_data\": \"$(sed ':a;N;$!ba;s/\n/\\n/g' config.yaml)\"}"
# hoặc form
curl -X POST http://localhost:8320/api/admin/v1/migration/yaml \
  -H "Authorization: Bearer $SMART_ROUTER_KEY" \
  --data-urlencode "yaml_data@config.yaml"
```

Response: `{revision_id, activated:true}`. Kiểm tra `GET /api/admin/v1/revisions/{id}` và `GET /api/admin/v1/routes`.

## 3. Overview

`GET /api/admin/v1/overview` trả `{providers, active_revision, revisions:{active}, usage, status}` — không chứa secret, graceful degrade khi DB down.

## 4. Health

`GET /health` → 200. Gateway readiness phụ thuộc postgres/redis healthcheck (`pg_isready`, `redis-cli ping`). Worker tự retry catalog sync.

## 5. Ledger (request/attempt/usage)

- List: `GET /api/admin/v1/ledger/requests?route_id=&logical_model=&limit=&offset=`
- Detail: `GET /api/admin/v1/ledger/requests/{request_id}` → `{request, attempts[], usages[]}`
- Stats: `GET /api/admin/v1/ledger/stats?start=&end=` → `{summary, by_provider}`
Mỗi request có `x-request-id`, mỗi attempt có `attempt_id`. Usage ghi `source/confidence`.

## 6. Audit

`GET /api/admin/v1/audit?action=&limit=&offset=` — trả audit redacted: `revision.activated`, `route.updated`, `provider.deleted`, `settings.updated`, `migration.yaml`, `credential.added`, `route.models.imported`. Không log plaintext secret.

## 7. Acceptance 1.0 (AC-01..16)

- AC-01 provider add/test/discover/import: `POST /providers`, `POST /providers/{id}/test`, `POST /providers/{id}/discover`, `POST /providers/{id}/models/import` (provider-agnostic driver).
- AC-02 không hardcode provider-name trong routing core (check `apps/gateway/routing/engine.py`).
- AC-03 multi-credential: `POST/GET /providers/{id}/credentials` (mã hoá Fernet).
- AC-14 mã hoá at-rest: `credential_encrypted` không trả qua API.
- AC-15 compose đầy đủ: `docker-compose.yml` + volumes + backup/restore (`docs/ops/backup-restore.md`).
- AC-16 legacy migration: `POST /migration/yaml`.

Chạy `pytest -q` phải xanh trước khi release (hiện 519 passed, 6 skipped).

## 8. Troubleshoot

- 401 admin: kiểm tra `SMART_ROUTER_KEY`.
- DB unreachable: overview `usage:null`, ledger 500 — kiểm tra `DATABASE_URL`, `docker compose logs postgres`.
- Redis unreachable: quota reservation fail-open — kiểm tra `REDIS_URL`.
- Migration 400: YAML không phải mapping hoặc thiếu `yaml_data`.
- Provider test 502: driver `validate_connection` lỗi — kiểm tra `base_url`/network.
- Secret lộ: kiểm tra mọi response/audit không chứa `sk-` hay plaintext key.

## 9. SLOs

Xem `docs/SLO.md` — availability 99.5%, p50 <200ms, p99 <2000ms, quota accuracy 100%, secret exposure 0.
