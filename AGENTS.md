# AGENTS.md — Eos

## What this is

Eos is a **multi-tenant RE photography SaaS** (Aryeo competitor): signup, subdomain tenants, platform billing, Stripe Connect client payments, DNS domain verification, platform admin ops.

Stack: **FastAPI + Jinja2 + HTMX**, SQLite WAL, port **8410**. Current version: **1.6.0**.

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

## SaaS production env

```bash
EOS_SAAS_MODE=true
EOS_SIGNUP_ENABLED=true
EOS_BASE_DOMAIN=yourdomain.com
EOS_BILLING_ENFORCE=true
EOS_STRIPE_PLATFORM_SECRET_KEY=sk_live_...
EOS_STRIPE_PLATFORM_WEBHOOK_SECRET=whsec_...
EOS_STRIPE_PRICE_STARTER=price_...
EOS_STRIPE_PRICE_PRO=price_...
EOS_PLATFORM_ADMIN_EMAILS=you@company.com
```

Use `deploy/Caddyfile` for `*.yourdomain.com` wildcard TLS. Studios connect Stripe at `/admin/stripe/connect`.

## Shipped (v1.6)

Phase 16 SaaS pivot: Stripe Connect, DNS domain verification, platform admin v2 (suspend/plan override), per-tenant storage metering + seat limits, marketing landing. Phases 1–15 prior. See CHANGELOG.md.