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
- **Booking** — packages, deposits, e-sign, twilight slots, homeowner flow, public referral links,
  embeddable iframe booking widget (`/book/embed`) with a per-studio frame allowlist
- **Pipeline** — listing tasks, Kanban, calendar, photographer assignment
- **Retention** — agent rebooking cockpit, agent growth panels, brokerage portfolio dashboard/portal, brokerage growth map, revenue optimizer, filtered acquisition tracking with referral attribution, intro/follow-up emails, per-agent summaries, follow-up queue, and repeat-agent reports
- **Delivery** — PIN galleries, property sites, agent portal, MLS/Zillow crops,
  auto-rendered 16:9 slideshow + 9:16 social reel MP4s (ffmpeg, background job)
- **Lead capture** — buyer lead form on property sites (honeypot + rate limited), durable
  agent/studio notification emails, admin leads inbox with CSV export and contacted tracking
- **Analytics** — privacy-preserving gallery/property-site view tracking, per-listing admin
  report with CSV export, portal view counts, and weekly agent traffic digest emails
- **Payments** — Connect onboarding at `/admin/stripe/connect`
- **Integrations** — Google Calendar, Dropbox ingest, API v1, webhooks
- **Platform trust** — public `/terms` + `/privacy` pages, per-token API and public-endpoint rate limiting

## Quick start

```bash
git clone https://github.com/Ayyitskevin/eos.git && cd eos
make install
cp .env.example .env          # EOS_SECRET_KEY + EOS_ADMIN_PASSWORD
make run                      # http://127.0.0.1:8410
make smoke                    # fast boot/migration/core-route smoke
make beta-smoke               # focused activation-to-retention journey
make test
```

**Try SaaS mode locally:** edit these values in `.env` (the launcher intentionally reads that file):

```bash
EOS_SAAS_MODE=true
EOS_SIGNUP_ENABLED=true
EOS_SIGNUP_AUTO_VERIFY_LOCAL=true  # local-only when no mail provider is configured
EOS_BASE_URL=http://localhost:8410
EOS_BASE_DOMAIN=localhost:8410
EOS_BILLING_ENFORCE=true
```

Then run `make run` and open <http://localhost:8410/signup>. Tenant URLs use
`http://{slug}.localhost:8410`; `127.0.0.1` is not an accepted SaaS Host.

### Beta journey (test mode only)

The current beta path is built around explicit, recoverable state transitions:

- Tenant routing rejects malformed or unknown hosts; public booking stays closed until the
  studio is active, verified, branded, published, and booking-enabled.
- Booking creation is atomic and keyed against form/API replay. Deposit-required requests stay
  `pending_payment` until a verified payment confirms them, including when online payments are
  unavailable.
- Gallery publication requires a linked, eligible listing, no open shoot/twilight appointment,
  at least one asset, and every asset in `ready` state.
- Automated email, SMS, and integration work is persisted before provider I/O. Definite failures
  remain visible for retry; ambiguous outcomes stay closed until the tenant records a provider check.
- Stripe webhooks remain signature-verified at their routes. Invoice payment additionally binds
  the event ID, Checkout session, amount, currency, studio, payment rail, and Connect destination;
  receipt replay cannot repeat a completed transition.
- Delivered-agent portals expose tenant-bound rebooking and referral links. Signup verification
  management requires the authenticated tenant owner, is cooldown-protected, and requires provider
  reconciliation after an ambiguous outcome; no management bearer is placed in a URL.
- Google operator sign-in uses the configured HTTPS apex callback only as a broker: it cannot set a
  session there, and a short-lived, nonce-bound, single-use handoff completes on the verified tenant
  Host with host-only cookies.

Follow [the beta runbook](docs/BETA_RUNBOOK.md) for the local self-hosted flow and recovery checks.
It uses synthetic data and Stripe test mode; it is not a production deployment or live-payments claim.

## Production (hosted platform)

```bash
deploy/install-user.sh          # rootless → ~/opt/eos, user systemd unit
nano ~/opt/eos/.env             # see deploy/env.production.example
systemctl --user restart eos
```

| Step | Detail |
|------|--------|
| DNS | `eos.yourdomain.com` + `*.eos.yourdomain.com` → Cloudflare Tunnel (TLS terminated at the edge) |
| Stripe | Platform billing + Connect; webhook → `/stripe/platform/webhook` |
| Media | `EOS_S3_*` for scale (recommended) |
| Readiness | `/healthz` for liveness, `/readyz` for load balancers |

The rootless + Cloudflare Tunnel path above is the primary topology. A classic VPS install with
Caddy/nginx and a DNS-01 wildcard certificate (`sudo INSTALL_CADDY=1 deploy/install.sh`) is
documented as an appendix in the deploy guide.

Guides: [docs/DEPLOY.md](docs/DEPLOY.md) · [docs/SCALE.md](docs/SCALE.md)

### Minimum SaaS env

```bash
EOS_SAAS_MODE=true
EOS_SIGNUP_ENABLED=true
EOS_SIGNUP_AUTO_VERIFY_LOCAL=false
EOS_BASE_DOMAIN=eos.yourdomain.com
EOS_BASE_URL=https://eos.yourdomain.com
EOS_BILLING_ENFORCE=true
EOS_COOKIE_SECURE=true
EOS_STRIPE_PLATFORM_SECRET_KEY=sk_live_...
EOS_STRIPE_PLATFORM_WEBHOOK_SECRET=whsec_...
EOS_STRIPE_PRICE_STARTER=price_...
EOS_STRIPE_PRICE_PRO=price_...
EOS_PLATFORM_ADMIN_EMAILS=you@yourdomain.com
EOS_EMAIL_PROVIDER=postmark
EOS_POSTMARK_API_KEY=replace-with-server-token
EOS_POSTMARK_FROM_EMAIL=notifications@yourdomain.com
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

**v1.10.0** · FastAPI · Jinja2 · HTMX · SQLite WAL · Stripe · Postmark · boto3 (prod)

## Platform admin

`EOS_PLATFORM_ADMIN_EMAILS` → `/admin/platform/studios`

Suspend tenants, override plans, view usage, impersonate (audit logged).

## Development

```bash
make lint          # ruff
make smoke         # fast boot/migration/core-route smoke
make beta-smoke    # focused beta journey smoke
make test          # full pytest suite
make coverage      # full suite + enforced coverage floor
make security      # Bandit + dependency vulnerability audit
make check-stripe  # verify test keys in .env
make dogfood       # seed 1420 Maple Dr
make check-env     # validate .env
```

### For AI agents & contributors

**Read [docs/AI_AGENTS.md](docs/AI_AGENTS.md) before changing code.** It documents tenant isolation rules, payment rails, migration patterns, and what not to break.

Short pointer: [AGENTS.md](AGENTS.md) ·
[Beta runbook](docs/BETA_RUNBOOK.md) ·
[MicroSaaS loop](docs/MICROSAAS_LOOP.md)

## Roadmap

- [x] Multi-tenant SaaS + Stripe Connect (v1.6)
- [x] S3/R2 + production deploy (v1.7)
- [x] Per-tenant email + invite-only beta (v1.8)
- [ ] PostgreSQL
- [x] Per-tenant transactional email (v1.8)
- [ ] Zillow Showcase / MLS connectors

## Links

| | |
|---|---|
| Repo | https://github.com/Ayyitskevin/eos |
| Changelog | [CHANGELOG.md](CHANGELOG.md) |
| Agent guide | [docs/AI_AGENTS.md](docs/AI_AGENTS.md) |
| Beta runbook | [docs/BETA_RUNBOOK.md](docs/BETA_RUNBOOK.md) |
| Deploy | [docs/DEPLOY.md](docs/DEPLOY.md) |
