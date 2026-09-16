# SLOs — Smart Router 1.0

## Tổng quan

Mục tiêu đo lường độ tin cậy và hiệu năng khi triển khai production self-hosted.

## SLOs chính

| Metric | Target | Window | SLI Method |
|--------|--------|--------|------------|
| Gateway availability | 99.5% | monthly | `GET /health` 200 rate |
| Request success rate (non-5xx) | 99.0% | monthly | HTTP status ≥200 && <500 across `/v1/messages` |
| P50 latency | <200ms | weekly | End-to-end request time measured by gateway middleware |
| P99 latency | <2000ms | weekly | End-to-end request time p99 distribution |
| Quota admission accuracy | 100% | continuous | No oversubscription in concurrent reservation tests |
| Secret exposure incidents | 0 | continuous | Scan logs/responses/audit for `sk-*` patterns; automated check in CI |
| Data durability | 100% | continuous | Ledger records survive restart (integration test) |

## SLAs nội bộ

- **Gateway downtime** tối đa 3.6 giờ/tháng (99.5%)
- **Budget violation incidents**: 0
- **Audit trail completeness**: 100% events logged within 1s of occurrence

## Monitoring endpoints

- `GET /health/live` — service process alive
- `GET /health/ready` — upstream connections reachable
- `GET /api/admin/v1/overview` — Control Plane aggregate
- `GET /api/admin/v1/ledger/stats` — usage volume
- `GET /api/admin/v1/alerts?severity=critical` — active critical alerts

## Alerting rules

1. `GET /health/live` trả về lỗi liên tục 30s → trigger critical alert
2. Gateway error rate >10% trong 5 phút → trigger warning
3. Quota oversubscription detected → trigger critical alert
4. Any `sk-*` pattern found in audit log → trigger critical (security incident)
5. Backup job failed last run → trigger warning

## Runbook xử lý cảnh báo

| Alert | Action | Escalation |
|-------|--------|------------|
| Health down | Check Docker/compose status; `docker compose logs gateway` | L1 ops immediately |
| Error rate >10% | Review `/api/admin/v1/audit`; check provider health | L1 then L2 if persists >15min |
| Quota oversubscribed | Check quota config; adjust limits or add providers | L2 ops |
| Secret leaked | Immediately rotate affected key; audit all API calls in window | Security team immediately |
| Backup failed | Run manual backup; check postgres/redis health | L1 ops |
