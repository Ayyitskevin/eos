# Eos work log

## 2026-06-28 — Phase 18 shipped + local beta dogfood

**Repo:** [github.com/Ayyitskevin/eos](https://github.com/Ayyitskevin/eos) · **Commit:** `c658559` on `main` · **Version:** v1.8.0 · **Tests:** 78 passing

### Shipped (Phase 18)

- **Per-tenant email** (`eos/mailer.py`) — Postmark API or Gmail SMTP; studio-branded From (`Summit Lens via Eos`) + Reply-To to studio contact; wired into sequences, delivery notify, manual sends, gallery emails; platform sender for signup verification
- **Invite-only beta signup** (`eos/invites.py`) — `EOS_SIGNUP_INVITE_ONLY=true` gates `/signup`; platform admin at `/admin/platform/invites`
- **Onboarding checklist** — new steps: Connect Stripe → Publish site & enable booking → Platform billing; shows booking link when ready
- **Public `/pricing`** on SaaS apex (Trial / Starter / Pro from `plan_limits.py`); marketing page links to it
- **Migration:** `0017_phase18_email_invites.sql`
- **Docs:** `CHANGELOG.md`, `README.md`, `AGENTS.md`, `docs/AI_AGENTS.md` updated to v1.8.0

### Local beta verification (same day)

Configured `.env` for SaaS mode and ran end-to-end flow on `127.0.0.1:8410`:

| Step | Result |
|------|--------|
| Server in SaaS mode (`saas=True`) | OK |
| Platform admin login | OK |
| Invite code `BETA2026` created (10 uses) | OK |
| Signup → studio **Summit Lens** (`summit-lens`) | OK, invite redeemed |
| Tenant login + onboarding checklist | OK |
| Platform admin blocked for studio owner | 403 (correct) |
| `/pricing` on apex | OK |

**Local credentials (dev only):**

- Platform admin: `owner@localhost` / `dogfood-admin` → http://127.0.0.1:8410/admin/platform/invites
- Beta studio: `kevin@summitlens.test` / `beta-pass-2026` → http://summit-lens.localhost:8410/admin/onboarding
- Invite code: `BETA2026` (9 uses remaining)

**Note:** Tenant login must use subdomain host (`slug.localhost:8410`); apex login scopes to `default` studio and won't find tenant users.

### Not done yet

- Production deploy on real VPS + domain
- Stripe live keys
- Postmark domain verification
- Real shoot dogfood (optional — booking/invoices testable in Stripe test mode)

### Next when ready

Production deploy via `deploy/install.sh` with real domain, DNS (`eos.domain` + `*.eos.domain`), Caddy wildcard TLS, and production `.env` from `deploy/env.production.example`.