# AI Agent Guide — Eos

**Read this before changing code.** Eos is a multi-tenant RE photography SaaS (Aryeo-class). The goal is to **extend** what works, not refactor or break tenant isolation.

**Version:** 1.8.0 · **Port:** 8410 · **Repo:** https://github.com/Ayyitskevin/eos

---

## Mission (do not drift)

| In scope | Out of scope |
|----------|--------------|
| Multi-tenant hosted SaaS for RE photographers | Generic CRM, wedding/F&B photography |
| Listing-centric pipeline (book → shoot → deliver) | Replacing SQLite without a migration plan |
| Stripe Connect (studios collect client $) | Single global Stripe for all tenant revenue |
| Self-hosted platform economics | Vendor-lock-in features |

**Product direction:** Compete with Aryeo/Spiro as an operator-hosted platform — signup, subdomains, platform billing, per-studio Connect, branded delivery.

---

## Non-negotiable invariants

Breaking these causes data leaks or production outages. **Never:**

1. **Query or mutate without `studio_id`** — use `STUDIO_ID` from `eos/vocab.py` in SQL params, or explicit `studio_id` when provisioning.
2. **Bypass tenant middleware** — `tenant.bind_request()` runs before routes; do not cache `studio_id` across requests.
3. **Mix payment rails** — platform subscriptions (`platform_billing.py`) ≠ client payments (`stripe_connect.py` + `stripe_checkout.py`) ≠ legacy solo key (`EOS_STRIPE_SECRET_KEY`).
4. **Auto-verify custom domains** — only set `custom_domain_verified=1` after `domain_verify.try_verify_saved()` succeeds.
5. **Commit secrets or data** — never `.env`, `data/`, or `backups/`.
6. **Delete or renumber migrations** — only append `eos/migrations/00NN_*.sql`.
7. **Drive-by refactors** — match existing module layout, naming, and Jinja/HTMX patterns.

---

## Request flow (middleware order)

```
HTTP request
  → request_id (main.py)
  → tenant_context: tenant.bind_request() → billing_gate → rbac (admin) → CSRF
  → route handler
```

### Tenant resolution (`eos/tenant.py`)

1. Platform admin impersonation cookie
2. `/demo` path → demo studio
3. Custom domain (verified only)
4. Subdomain `{slug}.{EOS_BASE_DOMAIN}`
5. Logged-in user's `users.studio_id`
6. Fallback: `"default"` (solo / apex marketing)

Inactive studios: subdomain still resolves; `billing_gate` returns 403 on public routes.

---

## Architecture map

```
eos/
├── main.py              # App entry, router includes, middleware
├── config.py            # All EOS_* env vars, APP_VERSION
├── db.py                # SQLite + numbered migrations
├── tenant.py            # Subdomain, custom domain, branding
├── vocab.py             # STUDIO_ID lazy binding, RE enums
├── security.py          # Auth, CSRF, rate limits
├── billing_gate.py      # Trial expiry, signup verify, suspend
├── plan_limits.py       # Trial/Starter/Pro caps — gate new features here
├── usage.py             # Metering (listings, storage, API calls)
│
├── platform_billing.py  # Studios pay Eos (Stripe subscriptions)
├── stripe_connect.py    # Studios connect Stripe Express
├── stripe_checkout.py   # Client invoice/deposit checkout (Connect or legacy)
├── object_store.py      # Optional S3/R2 sync
│
├── onboarding.py        # create_studio() provisioning
├── studio_seed.py       # Seed packages, presets, sequences per tenant
├── platform_admin.py    # Suspend, plan override, impersonation audit
│
├── routes/              # FastAPI routers (thin — logic in eos/*.py)
├── migrations/          # 0001–0016+, append only
└── templates/         # Jinja2 (admin/, site/, public/)
```

### Routes by surface

| Prefix | Module | Audience |
|--------|--------|----------|
| `/signup`, `/` (SaaS apex) | `routes/signup.py`, `routes/site.py` | Public marketing |
| `/book`, `/booking/{token}` | `routes/site.py`, `routes/booking.py` | Agent/homeowner booking |
| `/g/{slug}`, `/l/{slug}` | `routes/delivery.py`, `routes/microsites.py` | Gallery + property site |
| `/portal/{token}` | `routes/portal.py` | Agent portal |
| `/admin/*` | `routes/*_admin.py`, `studio_admin.py` | Studio operators |
| `/admin/platform/*` | `routes/platform_admin.py` | Platform super-admin |
| `/admin/stripe/connect` | `routes/stripe_connect.py` | Studio Connect onboarding |
| `/admin/billing` | `routes/platform_billing.py` | Studio platform subscription |
| `/api/v1/*` | `routes/api_v1.py` | Bearer tokens per studio |
| `/stripe/platform/webhook` | Platform billing + Connect account.updated |

---

## Domain model (listing-centric)

Everything hangs off **`listings`**:

```
clients → listings → galleries → sections → assets
                    → appointments, proposals, invoices, tasks
                    → property microsite (/l/{slug})
```

RE-specific vocabulary lives in `eos/vocab.py` — room sections, shot lists, MLS export channels. Do not add F&B or generic `shoot_type` enums.

---

## Plan tiers (`eos/plan_limits.py`)

| Tier | Listings/mo | Seats | Storage | Custom domain |
|------|-------------|-------|---------|---------------|
| solo | ∞ | ∞ | ∞ | yes |
| trial | 15 | 1 | 5 GB | no |
| starter | 30 | 1 | 25 GB | no |
| pro | ∞ | 5 | 100 GB | yes |

**When adding a gated feature:** add limit key to `LIMITS`, enforce in module, expose in `usage.snapshot()` and billing UI.

---

## Payment rails (three separate systems)

| Rail | Env / module | Money flow |
|------|--------------|------------|
| Platform subscription | `EOS_STRIPE_PLATFORM_*`, `platform_billing.py` | Studio → platform operator |
| Stripe Connect | `stripe_connect.py`, `stripe_checkout.py` | Client → studio account |
| Legacy solo | `EOS_STRIPE_SECRET_KEY` | Fallback for single-tenant dev |

Use `stripe_checkout.payments_configured()` for UI `payments_on` flags — not raw env checks.

---

## How to add a feature safely

### 1. Schema change

```bash
# Create eos/migrations/0017_your_feature.sql
# Use ALTER TABLE / CREATE TABLE IF NOT EXISTS
# Include studio_id on all tenant-owned rows
make migrate   # or boot app (auto-migrates)
```

### 2. Business logic

- Add module under `eos/` (not bloated route files)
- Import `STUDIO_ID` for queries
- Call `db.audit()` for admin mutations
- Enqueue long work via `jobs.enqueue()` — do not block requests

### 3. Route + template

- Router in `eos/routes/`, register in `main.py`
- Admin routes: `dependencies=[Depends(security.require_admin)]`
- Match existing Jinja patterns (`admin-main`, `card`, HTMX if adjacent routes use it)

### 4. Tests

- Add `tests/test_phaseNN.py` or extend existing phase test
- Use `tests/conftest.py` fixtures `app_env` / `app_env_http`
- SaaS tests: set `EOS_SAAS_MODE`, `EOS_BASE_DOMAIN`, use `onboarding.create_studio()`
- Run `make test` and `make lint` before push

### 5. Ship checklist

- [ ] `studio_id` on all new tables/queries
- [ ] Plan limit if resource is metered
- [ ] Migration appended (not edited)
- [ ] `CHANGELOG.md` entry if user-facing
- [ ] Bump `APP_VERSION` in `eos/config.py` + `pyproject.toml` for releases
- [ ] Push to `main` (see GitHub rules below)

---

## Stubs — extend, don't rip out

| Area | File | Current behavior |
|------|------|------------------|
| SMS | `eos/sms.py` | Logs stub when Twilio unset |
| MLS push | `routes/api_v1.py` | Logs payload only |
| AI cull | `eos/jobs.py` | Picks first photo per section |
| Postgres | `EOS_DATABASE_URL` | Documented only — SQLite is production default |

Replace stubs with real implementations behind existing env flags; keep no-op fallback when unset.

---

## Roadmap (prioritized — align new work here)

| Priority | Item | Notes |
|----------|------|-------|
| P0 | Production deploy on real domain | `docs/DEPLOY.md` |
| P0 | S3/R2 in production | `EOS_S3_*` — shipped, needs bucket |
| P1 | PostgreSQL backend | `docs/SCALE.md` — do not half-migrate |
| P1 | Per-tenant email polish | Postmark shipped — verify domain + bounce handling |
| P2 | Zillow Showcase / MLS connectors | Aryeo moat |
| P2 | Mobile agent PWA | Web portal exists |

**Do not start** mobile native apps, GraphQL rewrites, or framework migrations unless explicitly requested.

---

## Environment reference

| Variable | Required | Purpose |
|----------|----------|---------|
| `EOS_SECRET_KEY` | yes | Session signing (32+ chars) |
| `EOS_ADMIN_PASSWORD` | solo dev | Legacy admin; disabled in SaaS |
| `EOS_SAAS_MODE` | hosted | Enables signup, billing patterns |
| `EOS_BASE_DOMAIN` | SaaS | Tenant subdomains |
| `EOS_STRIPE_PLATFORM_SECRET_KEY` | SaaS billing | Platform subscriptions |
| `EOS_S3_BUCKET` | scale | Object storage sync |
| `EOS_PLATFORM_ADMIN_EMAILS` | ops | `/admin/platform/studios` |

Full lists: `.env.example`, `deploy/env.production.example`

Validate: `EOS_CHECK_MODE=production make check-env`

---

## GitHub workflow

1. **Push to `main` after every shipped change**
2. Never commit `.env`, `data/`, credentials
3. CI: ruff, pytest (55% coverage gate), bandit
4. If workflow YAML push fails: `gh auth refresh -s workflow` or GitHub MCP

---

## Commands

```bash
make install    # venv + dev deps
make run        # uvicorn :8410
make test       # pytest
make lint       # ruff
make dogfood    # seed 1420 Maple Dr pipeline
make check-env  # validate .env
```

SaaS local smoke:

```bash
EOS_SAAS_MODE=true EOS_SIGNUP_ENABLED=true EOS_BASE_DOMAIN=localhost:8410 make run
```

---

## Common mistakes (avoid)

| Mistake | Correct approach |
|---------|------------------|
| `db.one("SELECT ... WHERE id=?", (id,))` without studio | Add `AND studio_id=?` + `STUDIO_ID` |
| `config.BASE_URL` in tenant emails/links | `tenant.get_base_url()` |
| New migration editing old file | Append `0017_*.sql` |
| Open signup without rate limits | Use `security.signup_throttled()` |
| Storage upload without cap check | `usage.enforce_storage_limit()` |
| Platform admin without audit | `platform_admin.audit()` |

---

## Related docs

| Doc | Purpose |
|-----|---------|
| [README.md](../README.md) | Human-facing overview |
| [AGENTS.md](../AGENTS.md) | Short pointer (this file is canonical) |
| [DEPLOY.md](DEPLOY.md) | Production install |
| [SCALE.md](SCALE.md) | S3, Postgres roadmap |
| [CHANGELOG.md](../CHANGELOG.md) | Release history |

---

| `EOS_EMAIL_PROVIDER` | postmark/smtp | Transactional email |
| `EOS_SIGNUP_INVITE_ONLY` | beta | Require invite code at signup |

*Last updated: Phase 18 (v1.8.0). Update this file when architecture or invariants change.*