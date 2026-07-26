# Eos Beta Runbook

> Scope: local, self-hosted validation with synthetic studio/client data and Stripe test mode.
> This runbook does not claim a production deployment, production readiness, or approval to use
> live payment credentials.

Use this alongside the [Stripe test guide](STRIPE_TEST.md) and the
[AI/contributor invariants](AI_AGENTS.md). The beta goal is a recoverable journey from tenant
activation through repeat booking—not merely a successful page load.

## Beta acceptance map

| Area | Expected invariant |
|------|--------------------|
| Tenant activation | Malformed and unknown Hosts fail closed. A studio must be active, verified, branded, published, and booking-enabled before `/book` opens. |
| Booking | One tenant-bound request key creates at most one inquiry/listing/appointment/invoice set, including repeat or concurrent submission. A required but unavailable deposit remains `pending_payment`. |
| Delivery | First publication requires an eligible linked listing, no open shoot/twilight appointment, at least one asset, and all assets in `ready` state. |
| Outbound work | Sequence email, integration webhook, and gallery-delivery email intents persist before I/O and expose tenant-scoped status, attempts, and failed-item retry. |
| Payments | Routes verify Stripe signatures. Invoice effects require the exact event ID, Checkout session, amount, currency, studio, rail, and Connect destination, with a durable receipt for replay. |
| Retention | A delivered agent portal exposes a returning-client booking link and an attributed referral link without crossing tenant boundaries. |

## Safety boundaries

- Use a dedicated local data directory and synthetic addresses, people, inboxes, and images.
- Keep `.env` untracked. Never paste keys, webhook secrets, email credentials, or client data into
  commits, issue text, screenshots, or shared logs.
- Use only Stripe test-mode credentials (`sk_test_…`), test Connect accounts, and test cards. Never
  place an `sk_live_…` value in this local environment.
- Bind the app to loopback. `EOS_COOKIE_SECURE=false` is acceptable only for this local HTTP run.
- Run the supported service with exactly one application worker. Eos owns an in-process scheduler;
  the checked-in service units intentionally use `--workers 1` until scheduler leadership is moved
  to a durable external lease.
- Keep `EOS_STRIPE_SECRET_KEY` empty for a non-default SaaS studio. Client payments must use that
  studio's test Connect destination; the platform/legacy key is not a fallback.
- Send email and webhook tests only to inboxes/endpoints you control. Do not use real customers or
  production automation destinations.
- API tokens and webhook signing secrets are shown only in the direct, no-store creation response.
  Copy them then; reload clears the value, and Eos never places it in a redirect URL.
- Signup never places verification-management authority in a URL. A newly created owner signs in
  on the tenant Host before viewing, resending, or reconciling verification delivery.
- Google operator sign-in is deliberately unavailable in this runbook's plain-HTTP setup. Exercise
  it only behind controlled HTTPS with `EOS_COOKIE_SECURE=true`, an apex callback, and a resolvable
  tenant subdomain or verified custom domain; the apex must never receive an operator session.
- Do not deploy from this runbook. Production decisions still require the separate deployment,
  security, backup, DNS, TLS, and human-review gates.

## 1. Prepare an isolated environment

From the repository root:

```bash
make install
cp .env.example .env
```

Use a local configuration shaped like this; replace every placeholder locally:

```bash
EOS_HOST=127.0.0.1
EOS_PORT=8410
EOS_BASE_URL=http://127.0.0.1:8410
EOS_BASE_DOMAIN=localhost:8410
EOS_DATA_DIR=./data/beta-test
EOS_SECRET_KEY=replace-with-a-random-local-secret-at-least-32-characters
EOS_ADMIN_PASSWORD=replace-with-a-local-admin-password
EOS_COOKIE_SECURE=false
EOS_DEMO_ENABLED=false

EOS_SAAS_MODE=true
EOS_SIGNUP_ENABLED=true
EOS_SIGNUP_AUTO_VERIFY_LOCAL=true
EOS_SIGNUP_INVITE_ONLY=false
EOS_BILLING_ENFORCE=true

# Leave this blank for SaaS tenant client payments.
EOS_STRIPE_SECRET_KEY=
EOS_STRIPE_WEBHOOK_SECRET=

# Optional until exercising test-mode Connect/platform webhooks.
EOS_STRIPE_PLATFORM_SECRET_KEY=
EOS_STRIPE_PLATFORM_WEBHOOK_SECRET=
EOS_STRIPE_PRICE_STARTER=
EOS_STRIPE_PRICE_PRO=
```

`EOS_SIGNUP_AUTO_VERIFY_LOCAL=true` is an explicit local-only shortcut so this isolated smoke does
not send real mail; strict production validation rejects it. SaaS mode enforces billing access even
if `EOS_BILLING_ENFORCE=false`; keep the documented value `true` so the configuration is explicit.
A synthetic signup receives the normal 14-day trial. Complete
this isolated beta journey inside that trial, or configure a fully test-mode platform subscription. Do
not change billing rows or add live credentials to bypass the gate.

Load these values into the current shell before checking the general environment:

```bash
set -a
. ./.env
set +a
make check-env
```

`make check-stripe` validates the complete Stripe dogfood configuration, including both test-plan
price IDs. Run it only after adding a test-mode platform key/listener secret and filling the
Starter/Pro prices with test-mode values:

```bash
make check-stripe
```

It intentionally reports an incomplete environment while those optional payment values are blank.
It also exits nonzero for any non-`sk_test_` platform or legacy secret key; a warning is never
treated as test readiness.
Use a fresh terminal after the run if you do not want the sourced values to remain in your shell.

If no mail provider is configured, local signup auto-verifies the studio. That is useful for the
rest of the journey but does not exercise verification delivery or resend. To test those paths,
configure a controlled SMTP/Postmark test inbox before signup.

## 2. Start Eos and the Stripe test listener

For payment testing, start the listener in one terminal:

```bash
make stripe-listen
```

Copy the listener's `whsec_…` into `EOS_STRIPE_PLATFORM_WEBHOOK_SECRET`, then start or restart Eos
in another terminal:

```bash
make run
```

Use the apex URL for signup and the tenant Host for studio work:

- Platform/signup: `http://127.0.0.1:8410/signup`
- Example tenant: `http://my-studio.localhost:8410/admin/login`
- Public booking: `http://my-studio.localhost:8410/book`

Unknown tenant subdomains should return 404 and malformed Host headers should return 400. An apex
or `127.0.0.1` request resolves the platform/default tenant, not the new studio. If the browser does
not resolve `*.localhost`, use a local resolver entry approved for your machine or inspect routes
with an explicit `Host` header; do not weaken the application Host check.

## 3. Activate and open a studio

1. Sign up a synthetic studio at `/signup`; the response returns to the new tenant's login page
   without a verification-management token in the URL.
2. Sign in as the newly created owner on that tenant Host.
3. With a configured test mailer, confirm that protected admin routes redirect the authenticated
   owner to `/admin/verify-pending` until the 48-hour verification link is used.
4. If delivery failed, fix the test mailer and use **Resend verification email** while signed in as
   that owner. Resends have a five-minute cooldown and reuse the tenant's durable verification state.
5. After verification, open `/admin/onboarding` and use **Quick launch** to set a headline and
   service area and enable published booking. Review the
   seeded packages. The booking readiness check requires active + verified + headline/service area
   + published + booking-enabled state.

A wrong Host or incomplete activation must never expose that studio's booking form.

## 4. Exercise booking, offline deposits, and replay

1. Open the tenant's `/book` form and submit a synthetic agent booking. The form carries a hidden
   request key; API clients must supply their own bounded request key.
2. Repeat the same request key. The response should resolve to the existing booking rather than
   create another listing, appointment, inquiry, invoice, referral redemption, or automation.
3. For a package with no deposit, expect a confirmed inquiry/appointment and a booked listing.
4. For a package requiring a deposit while Connect is unavailable, expect an invoice plus a
   `pending_payment` inquiry and proposed appointment. The slot is not confirmed and the UI has no
   online Pay action. Do not add a legacy platform key to force payment.
5. To complete that deposit path, connect the studio's Stripe test account at
   `/admin/stripe/connect`, reopen the public invoice/booking link, and pay with a Stripe test card.
   Only the signed, exactly bound webhook may confirm the deposit workflow.
   A first Checkout is refused when fewer than 30 minutes remain on the payment hold; create a new
   booking instead of extending or editing the old hold.

The normal successful test card is `4242 4242 4242 4242` with any future expiry/CVC/ZIP. See the
[Stripe test guide](STRIPE_TEST.md) for the declined-card case and Connect setup.

## 5. Exercise the shoot-to-delivery gate

1. Open `/admin/calendar`, edit the linked shoot/twilight appointment, and mark the completed shoot
   `completed` (an explicitly canceled shoot is also closed, but is not the normal delivery path).
2. Link a gallery to the listing and upload at least one synthetic image.
3. Wait until every gallery asset reports `ready`. Pending or failed assets keep publication
   disabled.
4. Open `/admin/galleries/{gallery_id}`. Its readiness message should identify any remaining block:
   missing/Lead/Archived listing, open shoot, no assets, or pending/failed assets.
5. Publish only when the page reports **Ready to publish and deliver**.

First publication atomically records delivery state and durable downstream intents. The delivered
agent portal should then expose **Book your next listing** and, when a code exists, the tenant's
`/r/CODE` referral link. Following that short link must return to the same tenant's booking form
with attribution and credit context.

## 6. Operator recovery

The scheduler scans pending work and stale claims. Manual retry is for visible, definitely `failed`
work after the underlying cause is fixed; it does not create a second intent. A timeout, lost worker,
or database failure after provider dispatch has an **unknown** outcome. Check the controlled provider
first, then use the explicit reconciliation control to record delivered/not delivered. Never turn an
unknown claim back into pending merely to make it run again.

| Symptom | Inspect | Safe operator action |
|---------|---------|----------------------|
| Unknown or wrong tenant | Request Host and `/admin/studio#domain` | Correct the Host/base-domain setup or complete custom-domain verification. Do not add a permissive fallback. |
| Verification delivery failed/unknown | Authenticated owner at `/admin/verify-pending` | For a definite failure, fix the controlled provider and use **Resend verification email** after the cooldown. For an unknown/stale claim, check the provider first and record delivered/not delivered. Never share a URL as verification-management authority. |
| Deposit request remains pending | Booking confirmation, inquiry/invoice in `/admin/studio`, Connect status | Finish test Connect onboarding, then use the bound public invoice flow. Do not substitute the legacy platform key. |
| Checkout claim has no saved session | Pending hold reconciliation error in `/admin/studio#operations` | Verify test Stripe configuration, then reopen the same public invoice and choose Pay again. The deterministic provider idempotency key recovers the accepted Checkout if one exists. If the provider remains unreachable, leave the hold unchanged and inspect test-mode Stripe request logs; do not clear the claim in SQLite. |
| Record an offline/manual payment | Admin invoice detail | Use **Mark paid** only after the payment is independently verified. Eos first expires an open bound Checkout or reconciles a paid one; provider errors and unresolved claims block the manual transition. |
| Payment hold is stale | `/admin/studio#operations` → **Pending payment holds** | After its recorded expiry, click **Release expired holds**. This cancels only elapsed unpaid holds and releases their proposed slot/referral reservation. |
| Gallery cannot publish | `/admin/galleries/{gallery_id}` | Follow the displayed readiness reason: link an eligible listing, complete the shoot, upload an asset, or repair/wait for asset processing. |
| Sequence email failed | `/admin/sequences` | Fix the mail provider/template/recipient. Retry only a definite failure; for an unknown/stale claim, verify the provider first and use the labeled operator action. |
| Integration webhook failed | `/admin/studio#integrations` → **Recent webhook deliveries** | Fix the controlled endpoint, then click **Retry**. The stored payload/event key is reused. |
| Gallery notification failed | `/admin/studio#integrations` → **Gallery delivery notifications** | Fix the test recipient/provider. Retry only after the provider confirms it did not accept the first send. Publication does not need to be repeated. |
| SMS reminder is failed/unknown | `/admin/studio#integrations` → **SMS reminders** | Check Twilio by appointment/date first. Use **Retry after Twilio check** only when Twilio confirms no delivery; the durable reminder intent prevents scheduler-tick duplicates. |
| Rebooking email is claimed/unknown | Agent detail → **Rebooking outreach** | Check the mail provider. Record **Provider shows delivered** or **Provider shows not sent — allow retry**; concurrent clicks and stale claims must not send again automatically. |
| Acquisition intro/follow-up is claimed/unknown | `/admin/reports/acquisition` → **Email delivery review** | Check the mail provider, then record **Delivered** or **Not delivered**. A fresh active claim cannot be reconciled or resent. |
| Google/Dropbox intent is unresolved | `/admin/studio#integrations` | Inspect the durable intent and provider identity. Reconcile the existing remote event/file; do not create a second remote object to clear local state. |
| Background job failed | `/admin/studio#operations` → **Failed background jobs** | Fix the underlying image/export/integration cause, then click **Retry** for that tenant-owned job. A retry resets its attempt budget but reuses the same durable job row. |
| Stripe signature rejected | Eos logs and listener secret | Correct the endpoint-specific `whsec_…`, restart Eos, and resend the signed test event. No receipt/invoice mutation should exist for an unverified request. |
| Stripe receipt is `rejected` | Eos logs and read-only receipt query below | Treat the mismatch as immutable. Correct the Checkout/invoice setup and generate a valid test event; do not edit the invoice or receipt to bypass binding. |
| Stripe receipt is `failed` | Eos logs and read-only receipt query below | Fix the transient downstream cause, then replay the same signed test event ID. The failed transactional effects are retried; processed receipts remain deduplicated. |

There is intentionally no tenant UI for mutating Stripe receipts. Read them without modifying the
database (the path below matches the sample `EOS_DATA_DIR`):

```bash
sqlite3 ./data/beta-test/eos.db \
  "SELECT source,event_id,event_type,status,attempts,studio_id,error,updated_at FROM stripe_event_receipts ORDER BY updated_at DESC LIMIT 20;"
```

Do not delete receipts, change invoice status, or rewrite destination/session bindings as a
recovery shortcut.

## 7. Verification commands

Run the repository gates from the root after the manual test-mode journey:

```bash
make smoke
make beta-smoke
make test
make lint
make coverage
make security
```

- `make smoke` checks boot, migrations, core routes, and SQL guardrails.
- `make beta-smoke` exercises the focused activation-to-retention journey.
- `make test` runs the full regression suite.
- `make lint` checks Ruff diagnostics and formatting.
- `make coverage` reruns the suite and enforces the configured coverage floor.
- `make security` runs Bandit and audits pinned runtime dependencies for known vulnerabilities.

Record each command and its observed result. Any failure is a beta hold until explained and fixed;
these commands do not themselves authorize production deployment or live payments.
