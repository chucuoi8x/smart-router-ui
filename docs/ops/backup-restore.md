# Backup and restore — Smart Router 1.0

## PostgreSQL

Create backup from host:

```bash
docker compose exec -T postgres pg_dump -U postgres -d smart_router -Fc > backups/db/smart_router_$(date +%Y%m%d_%H%M%S).dump
```

Restore into a stopped/maintenance stack:

```bash
docker compose stop gateway worker
docker compose exec -T postgres pg_restore -U postgres -d smart_router --clean --if-exists < backups/db/<backup>.dump
docker compose start gateway worker
```

## Redis

Redis runtime state is persisted by the `redis_data` volume. Create an RDB copy:

```bash
docker compose exec -T redis redis-cli BGSAVE
docker cp $(docker compose ps -q redis):/data/dump.rdb backups/redis/dump_$(date +%Y%m%d_%H%M%S).rdb
```

Restore by stopping Redis, replacing `dump.rdb`, and starting the service. Do not restore Redis without matching PostgreSQL state when quota/accounting consistency matters.

## Encryption key

Back up `SMART_ROUTER_ENCRYPTION_KEY` separately in a secret manager. Losing it makes encrypted provider credentials unrecoverable. Never commit `.env` or key material.
