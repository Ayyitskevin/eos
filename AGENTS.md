# AGENTS.md — Eos

## What this is

Eos is a **real estate photography** studio OS: listing pipeline, RE shot lists, room-section galleries, MLS export presets, multi-tenant SaaS.

Stack: **FastAPI + Jinja2 + HTMX**, SQLite WAL, port **8410**. Current version: **1.5.0**.

## Key paths

| Path | Purpose |
|------|---------|
| `eos/main.py` | App entry, middleware (tenant, billing, CSRF, request ID) |
| `eos/tenant.py` | Subdomain + session tenant resolution |
| `eos/security.py` | Auth, CSRF, rate limits, tenant-bound `require_admin` |
| `eos/migrations/` | Numbered SQL migrations |
| `tests/conftest.py` | Shared `app_env` / `app_env_http` fixtures |
| `Makefile` | `make test`, `make lint`, `make run`, `make dogfood` |
| `scripts/check-env.py` | Validate env before prod boot |
| `deploy/` | systemd, nginx, Caddy, backup, install |

## Conventions

- **Listing-centric** — everything hangs off `listings`
- **Tenant isolation** — all queries/mutations must include `studio_id`
- **Minimal diffs**; match existing module layout
- Run `make lint` and `make test` before pushing

## Running locally

```bash
make install   # or: pip install -r requirements-dev.txt
make run       # uvicorn with reload
make test
make dogfood   # seed 1420 Maple Dr
```

Required env: `EOS_SECRET_KEY`, `EOS_ADMIN_PASSWORD`. Data defaults to `./data`.

## GitHub

Repo: https://github.com/Ayyitskevin/eos — **push to `main` after every shipped change**. Never commit `.env` or `data/`. CI runs ruff, pytest+coverage, bandit. If workflow push fails, use GitHub MCP or `gh auth refresh -s workflow`.

## Shipped (v1.5)

Phases 1–15. Phase 15: drive-time scheduling, video delivery, homeowner booking, Google admin OAuth, platform impersonation, demo sandbox, QuickBooks export, Sentry hook. See CHANGELOG.md.