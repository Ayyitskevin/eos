# Changelog

All notable Eos releases. Version numbers match `eos/config.py` `APP_VERSION`.

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