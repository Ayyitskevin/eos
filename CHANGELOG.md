# Changelog

All notable Eos releases. Version numbers match `eos/config.py` `APP_VERSION`.

## Unreleased

- Hardened the beta journey with strict tenant Host/activation gates, atomic request-key booking,
  pending offline deposits, and delivery publication gated on completed shoots and ready assets.
- Signup verification, sequence, integration webhook, gallery-delivery, rebooking, acquisition,
  and SMS work now uses durable replay-safe intents. Definite failures can be retried; ambiguous
  outcomes stay blocked until a tenant-scoped provider reconciliation records the outcome.
- Stripe invoice payments now require signed, exact session/amount/currency/studio/rail/destination
  binding and durable event receipts for replay and failed-effect recovery; SaaS tenants cannot
  fall back to the legacy payment key.
- Delivered agent portals now lead into tenant-bound rebooking and attributed referral booking;
  added a local test-mode beta runbook and focused `make beta-smoke` entry point.
- Agent rebooking cockpit ranks inactive agents by prior listing value and links directly into a preselected new-listing flow.
- Manual rebooking outreach sends or drafts cooldown-safe agent follow-up emails through durable claims.
- Rebooking performance snapshot shows ready nudges, recent outreach, converted listings, and prior client value.
- Rebooking follow-up queue flags agents nudged 7+ days ago without a repeat listing.
- Reports now include repeat-agent revenue with brokerage attribution and CSV export.
- Brokerage account dashboard shows assigned agents, listing volume, paid value, open balance, recent listings, and CSV export.
- Brokerage portal now gives brokerages a self-serve account summary with invoice status, agent activity, property sites, and gallery links.
- Revenue optimizer reports package performance, listing-type value, add-on attach rate, missed upsells, and CSV export.
- Agent acquisition report tracks referral code performance, referred listing value, and high-value agents ready for an intro ask.
- Acquisition queue adds one-click referral introduction emails with draft fallback, cooldown tracking,
  durable claims, and provider-outcome reconciliation.
- Studio settings now summarize per-agent referral performance and acquisition CSV exports include referrer/contact outreach fields.
- Acquisition queue filters and bulk intro-send/draft actions help studios work high-value referral asks faster.
- Acquisition attribution now connects referral booking links to codes, referrers, brokerages, listings, and paid/open value.
- Brokerage accounts now include a growth map with anchor-office, penetration, referral, and next-action signals.
- Agent records now include a growth panel with revenue, referral, brokerage, and next-action signals.
- Acquisition now surfaces stale referral intro asks for second-touch follow-up emails with cooldown tracking.
- Public referral links now support `/r/CODE` short links and show applied referral credit on booking.
- Added `docs/MICROSAAS_LOOP.md` so future Eos work stays focused on real-estate photography MicroSaaS value.
- Updated Pillow to 12.3.0 to incorporate the current image-parser security fixes.
- Contract acceptance now has one atomic first signer and snapshots the tenant studio identity;
  equal-timestamp subscription updates deterministically keep the more restrictive status/tier.
- Signup consumes rate-limit capacity before all validation and requires an authenticated tenant
  owner for verification resend/reconciliation instead of exposing a reusable URL capability.
- Proxy templates overwrite a dedicated trusted client-IP header, reject unknown-host redirects,
  and bind the app to loopback; tenant scheduling, Google Calendar, and shoot-day SMS now use each
  studio's timezone with conservative DST handling.
- Google operator login now claims short-lived state on the apex before provider I/O, transfers a
  single-use fragment capability back to the initiating tenant, and creates only host-only Secure
  session cookies on that verified tenant Host.

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
- Inactive studio subdomain returns a privacy-preserving 404 on public routes

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
