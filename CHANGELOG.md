# Changelog

All notable Eos releases. Version numbers match `eos/config.py` `APP_VERSION`.

## Unreleased

- Agent rebooking cockpit ranks inactive agents by prior listing value and links directly into a preselected new-listing flow.
- Manual rebooking outreach sends or drafts cooldown-safe agent follow-up emails without adding a new table.
- Rebooking performance snapshot shows ready nudges, recent outreach, converted listings, and prior client value.
- Rebooking follow-up queue flags agents nudged 7+ days ago without a repeat listing.
- Reports now include repeat-agent revenue with brokerage attribution and CSV export.
- Added `docs/MICROSAAS_LOOP.md` so future Eos work stays focused on real-estate photography MicroSaaS value.

## 1.9.0 — Phase 19 (Stripe test-mode dogfood)

- Platform webhook handles Connect client `checkout.session.completed` (invoice/deposit payments)
- Shared `stripe_webhooks` module; legacy `/stripe/webhook` uses same handler
- Auto-refresh Connect account status on onboarding return (`?thanks=1`)
- `docs/STRIPE_TEST.md`, `make check-stripe`, `make stripe-listen`, `scripts/check-stripe-env.py`

## 1.8.0 — Phase 18 (email + beta signup)

- Per-tenant transactional email — Postmark API or Gmail SMTP; studio-branded From + Reply-To
- Invite-only signup (`EOS_SIGNUP_INVITE_ONLY`) with platform admin invite codes
- Onboarding checklist: Stripe Connect, publish/booking, platform billing
- Public `/pricing` page; marketing links to pricing
- Platform admin `/admin/platform/invites`

## 1.7.0 — Phase 17 (production + scale)

- S3/R2 object storage sync for gallery uploads and derivatives (`EOS_S3_*`)
- Production install hardening — systemd ExecStartPre env check, prod requirements
- `docs/DEPLOY.md` and `docs/SCALE.md` — hosted SaaS runbooks
- `deploy/env.production.example` defaults to SaaS mode
- README repositioned as open Aryeo-class multi-tenant platform
- `docs/AI_AGENTS.md` — canonical guide for AI agents (invariants, architecture, safe extension)

## 1.6.0 — Phase 16 (SaaS pivot)

- Stripe Connect — per-tenant client payments (deposits, invoices, upsells)
- DNS custom domain verification (CNAME or TXT) before routing
- Platform admin v2 — suspend/reactivate, plan override, usage stats, audit log
- Per-tenant storage metering with plan caps; team seat limits
- SaaS marketing landing on apex; signup copy updated for hosted platform
- Inactive studio subdomain returns 403 on public routes

## 1.5.0 — Phase 15 (low priority + ops)

- Drive-time aware scheduling with geocoding
- Video upload and gallery playback
- Homeowner booking flow (`/book/homeowner`)
- Google OAuth operator login
- Platform admin impersonation (`/admin/platform/studios`)
- Demo sandbox studio (`/demo`)
- QuickBooks CSV export and photographer pay tracking
- AI cull job stub, MLS export webhook stub
- Sentry integration hook, enhanced health checks, cron backup script

## 1.4.0 — Phase 14 (scale & retention)

- Agent self-reschedule, team calendar colors, agent credits
- Dropbox scan-now/retry, signup verification, onboarding wizard
- CSV report export, granular RBAC, Twilio SMS
- Brokerage portal, churn alerts, studio-namespaced media paths

## 1.3.0 — Phase 13 (operations & monetization)

- Visual calendar with drag reschedule and Google busy blocks
- Agent favorites and revision rounds
- Plan-tier limits and usage metering
- Custom domain routing, API writes, inbound webhooks
- Delivery upsell checkout, IPTC/EXIF on MLS exports

## 1.2.0 — Phase 12 (hardening)

- Tenant-bound admin sessions, CSRF, billing enforcement
- Integration event log, API pagination, backup script

## 1.0.0 — Phases 1–11

- Listing-centric RE pipeline, galleries, booking, portal, microsites
- Multi-tenant SaaS, Stripe billing, Google Calendar, Dropbox ingest
