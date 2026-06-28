# Scaling Eos

## Current architecture (v1.7)

| Layer | Default | Scale path |
|-------|---------|------------|
| Database | SQLite WAL | `EOS_DATABASE_URL` → PostgreSQL (planned) |
| Media | Local disk + optional S3/R2 sync | `EOS_S3_*` env vars |
| App | Single uvicorn, 2 workers | Horizontal replicas + shared DB/S3 |
| Jobs | In-process thread pool | Redis queue (planned) |

## S3 / R2 (shipped)

Set in production `.env`:

```bash
EOS_S3_BUCKET=eos-media
EOS_S3_REGION=auto
EOS_S3_ENDPOINT=https://<account>.r2.cloudflarestorage.com  # R2 only
EOS_S3_ACCESS_KEY=...
EOS_S3_SECRET_KEY=...
EOS_S3_PREFIX=eos
```

Uploads and image derivatives sync to `eos/media/{studio_id}/{gallery_id}/`. Usage metering reads S3 prefix sizes when configured.

## PostgreSQL (roadmap)

SQLite handles early SaaS (under ~25 active studios). For larger scale:

1. Set `EOS_DATABASE_URL=postgresql://...`
2. Run schema export/migration tooling (Phase 18)
3. Move to connection pooling (PgBouncer)
4. Run multiple app instances behind load balancer

Until Postgres ships, use nightly `deploy/backup.sh` and monitor disk IOPS.

## When to upgrade

| Signal | Action |
|--------|--------|
| 10+ studios, 50GB+ media | Enable S3/R2 |
| 25+ studios or write contention | Plan Postgres migration |
| Support load | Add platform admin runbook, Sentry |