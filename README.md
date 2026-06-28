# Eos

**Open multi-tenant SaaS for real estate photography studios** — the Aryeo-class alternative you host yourself.

Booking → shoot pipeline → branded delivery → agent portals → Stripe Connect payments. Each studio gets `{slug}.yourdomain.com`, optional custom domain, and collects client revenue on their own Stripe account.

[![CI](https://github.com/Ayyitskevin/eos/actions/workflows/ci.yml/badge.svg)](https://github.com/Ayyitskevin/eos/actions/workflows/ci.yml)

## Why Eos

| | Aryeo / Spiro | Eos |
|---|---------------|-----|
| Hosting | Their cloud | **Your infrastructure** |
| Client payments | Platform wallet or per-delivery fees | **Stripe Connect per studio** |
| Data ownership | Vendor lock-in | **SQLite + S3 you control** |
| Pricing | $0–$179/mo + add-ons | **You set platform plans** |
| RE workflow | Media delivery first | **Listing-centric pipeline end-to-end** |

Built for operators who want SaaS economics without surrendering the stack.

## Features

- **Multi-tenant SaaS** — signup, subdomains, 14-day trial, Starter/Pro plans, billing enforcement
- **Stripe Connect** — deposits, invoices, delivery upsells go to each studio's account
- **Booking** — packages, add-ons, e-sign, deposits, twilight slots, homeowner flow
- **Pipeline** — listing tasks, Kanban, calendar, photographer assignment, drive-time scheduling
- **Delivery** — PIN galleries, property microsites, agent portal, MLS/Zillow export crops
- **Ops** — proposals, contracts, sequences, webhooks, API v1, platform admin console
- **Scale** — S3/R2 media sync, per-tenant storage metering, seat limits

## Quick start (local)

```bash
git clone https://github.com/Ayyitskevin/eos.git && cd eos
make install
cp .env.example .env   # set EOS_SECRET_KEY, EOS_ADMIN_PASSWORD
make run               # http://127.0.0.1:8410
make test
```

**SaaS mode locally:**

```bash
EOS_SAAS_MODE=true EOS_SIGNUP_ENABLED=true EOS_BASE_DOMAIN=localhost:8410 make run
# Signup: http://127.0.0.1:8410/signup
```

## Production deploy (hosted platform)

```bash
sudo INSTALL_CADDY=1 deploy/install.sh
# Edit /opt/eos/.env — see deploy/env.production.example
sudo systemctl restart eos
```

Full guide: [docs/DEPLOY.md](docs/DEPLOY.md) · Scaling: [docs/SCALE.md](docs/SCALE.md)

### Minimum SaaS `.env`

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
EOS_S3_BUCKET=your-bucket          # recommended
```

DNS: apex + `*.eos.yourdomain.com` → server. Caddy config: `deploy/Caddyfile`.

## Architecture

```
                    ┌─────────────────────────┐
                    │  Caddy (*.BASE_DOMAIN)  │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  FastAPI (tenant middleware) │
                    │  SQLite · local/S3 media   │
                    └───────────┬─────────────┘
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
   Platform billing      Stripe Connect         S3/R2 sync
   (studios → you)       (clients → studio)     (media scale)
```

**Stack:** FastAPI · Jinja2 · HTMX · SQLite WAL · Stripe · optional boto3/S3

**Version:** 1.7.0 · Port **8410**

## Platform admin

Set `EOS_PLATFORM_ADMIN_EMAILS` → `/admin/platform/studios`

- List tenants, usage, storage
- Suspend / reactivate
- Plan override
- Impersonate studio (audit logged)

## Studio onboarding flow

1. Signup at `/signup` → `{slug}.yourdomain.com`
2. Verify email → onboarding wizard
3. Connect Stripe at `/admin/stripe/connect`
4. Subscribe (Starter/Pro) at `/admin/billing`
5. Publish site, enable `/book`, shoot first listing

## Development

```bash
make lint          # ruff
make test          # pytest (70+ tests)
make dogfood       # seed 1420 Maple Dr pipeline
make check-env     # validate .env
```

## Roadmap

- [x] Multi-tenant SaaS + Stripe Connect (v1.6)
- [x] S3/R2 media + production deploy docs (v1.7)
- [ ] PostgreSQL backend
- [ ] Per-tenant transactional email (Postmark/SES)
- [ ] Zillow Showcase / MLS connectors
- [ ] Mobile agent PWA

## License

Private / all rights reserved — contact maintainer for commercial hosting terms.

## Links

- **Repo:** https://github.com/Ayyitskevin/eos
- **Changelog:** [CHANGELOG.md](CHANGELOG.md)
- **Agent notes:** [AGENTS.md](AGENTS.md)