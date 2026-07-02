# Eos

**Open multi-tenant SaaS for real estate photography studios** — host your own Aryeo-class platform.

Studios get `{slug}.yourdomain.com`, branded booking and delivery, agent portals, and **Stripe Connect** so they collect client payments on their own accounts. You run the infrastructure and set platform subscription plans.

[![CI](https://github.com/Ayyitskevin/eos/actions/workflows/ci.yml/badge.svg)](https://github.com/Ayyitskevin/eos/actions/workflows/ci.yml)

| | Aryeo / Spiro | Eos |
|---|---------------|-----|
| Hosting | Vendor cloud | **Your server** |
| Client payments | Platform wallet or per-listing fees | **Stripe Connect per studio** |
| Data | Vendor-held | **SQLite + S3 you control** |
| Workflow | Delivery-first | **Listing pipeline end-to-end** |

## What studios get

- **Signup & subdomain** — 14-day trial, Starter/Pro plans, billing enforcement
- **Booking** — packages, deposits, e-sign, twilight slots, homeowner flow
- **Pipeline** — listing tasks, Kanban, calendar, photographer assignment
- **Retention** — agent rebooking cockpit, cooldown-safe outreach, follow-up queue, and conversion signals
- **Delivery** — PIN galleries, property sites, agent portal, MLS/Zillow crops
- **Payments** — Connect onboarding at `/admin/stripe/connect`
- **Integrations** — Google Calendar, Dropbox ingest, API v1, webhooks

## Quick start

```bash
git clone https://github.com/Ayyitskevin/eos.git && cd eos
make install
cp .env.example .env          # EOS_SECRET_KEY + EOS_ADMIN_PASSWORD
make run                      # http://127.0.0.1:8410
make smoke                    # fast boot/migration/core-route smoke
make test
```

**Try SaaS mode locally:**

```bash
EOS_SAAS_MODE=true EOS_SIGNUP_ENABLED=true EOS_BASE_DOMAIN=localhost:8410 make run
```

Open http://127.0.0.1:8410/signup

## Production (hosted platform)

```bash
sudo INSTALL_CADDY=1 deploy/install.sh
sudo nano /opt/eos/.env       # see deploy/env.production.example
sudo systemctl restart eos
```

| Step | Detail |
|------|--------|
| DNS | `eos.yourdomain.com` + `*.eos.yourdomain.com` → server |
| TLS | `deploy/Caddyfile` (wildcard) |
| Stripe | Platform billing + Connect; webhook → `/stripe/platform/webhook` |
| Media | `EOS_S3_*` for scale (recommended) |
| Readiness | `/healthz` for liveness, `/readyz` for load balancers |

Guides: [docs/DEPLOY.md](docs/DEPLOY.md) · [docs/SCALE.md](docs/SCALE.md)

### Minimum SaaS env

```bash
EOS_SAAS_MODE=true
EOS_SIGNUP_ENABLED=true
EOS_BASE_DOMAIN=eos.yourdomain.com
EOS_BASE_URL=https://eos.yourdomain.com
EOS_BILLING_ENFORCE=true
EOS_COOKIE_SECURE=true
EOS_STRIPE_PLATFORM_SECRET_KEY=sk_live_...
EOS_STRIPE_PRICE_STARTER=price_...
EOS_STRIPE_PRICE_PRO=price_...
EOS_PLATFORM_ADMIN_EMAILS=you@yourdomain.com
EOS_S3_BUCKET=your-bucket
```

## Architecture

```
  Caddy (*.BASE_DOMAIN)
         │
  FastAPI + tenant middleware
         │
    SQLite ──────► optional S3/R2 media sync
         │
  ┌──────┴──────┬──────────────┐
  Platform      Stripe         Object
  billing       Connect        storage
```

**v1.9.0** · FastAPI · Jinja2 · HTMX · SQLite WAL · Stripe · Postmark · boto3 (prod)

## Platform admin

`EOS_PLATFORM_ADMIN_EMAILS` → `/admin/platform/studios`

Suspend tenants, override plans, view usage, impersonate (audit logged).

## Development

```bash
make lint          # ruff
make smoke         # fast boot/migration/core-route smoke
make test          # full pytest suite
make check-stripe  # verify test keys in .env
make dogfood       # seed 1420 Maple Dr
make check-env     # validate .env
```

### For AI agents & contributors

**Read [docs/AI_AGENTS.md](docs/AI_AGENTS.md) before changing code.** It documents tenant isolation rules, payment rails, migration patterns, and what not to break.

Short pointer: [AGENTS.md](AGENTS.md) · MicroSaaS loop: [docs/MICROSAAS_LOOP.md](docs/MICROSAAS_LOOP.md)

## Roadmap

- [x] Multi-tenant SaaS + Stripe Connect (v1.6)
- [x] S3/R2 + production deploy (v1.7)
- [x] Per-tenant email + invite-only beta (v1.8)
- [ ] PostgreSQL
- [ ] Per-tenant transactional email
- [ ] Zillow Showcase / MLS connectors

## Links

| | |
|---|---|
| Repo | https://github.com/Ayyitskevin/eos |
| Changelog | [CHANGELOG.md](CHANGELOG.md) |
| Agent guide | [docs/AI_AGENTS.md](docs/AI_AGENTS.md) |
| Deploy | [docs/DEPLOY.md](docs/DEPLOY.md) |
