# AGENTS.md — Eos

## What this is

Eos is a **real estate photography** studio OS: listing pipeline, RE shot lists, room-section galleries, MLS export presets, multi-tenant SaaS (v1.2).

Stack: **FastAPI + Jinja2 + HTMX**, SQLite WAL, port **8410**.

## Key paths

| Path | Purpose |
|------|---------|
| `eos/main.py` | App entry, middleware (tenant, billing, CSRF, request ID) |
| `eos/tenant.py` | Subdomain + session tenant resolution |
| `eos/security.py` | Auth, CSRF, rate limits, tenant-bound `require_admin` |
| `eos/billing_gate.py` | SaaS billing enforcement |
| `eos/integrations/` | Google Calendar, Dropbox |
| `eos/platform_billing.py` | Per-studio Stripe subscriptions |
| `eos/migrations/` | Numbered SQL migrations |

## Conventions

- **Listing-centric** — everything hangs off `listings`
- **Tenant isolation** — all queries/mutations must include `studio_id`; admin sessions bound to tenant
- **Minimal diffs**; match existing module layout

## Running locally

```bash
uvicorn eos.main:app --host 127.0.0.1 --port 8410
pytest tests/ -q
```

Required env: `EOS_SECRET_KEY`, `EOS_ADMIN_PASSWORD`. Data defaults to `./data`.

## Shipped (v1.4)

Phases 1–14. Phase 14: portal reschedule, credits, RBAC, SMS, brokerage portal, churn alerts, media namespace. See README.