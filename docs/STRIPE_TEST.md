# Stripe test mode — local dogfood

Use Stripe **test mode** keys in `.env` to exercise platform billing and Connect client payments without going live.

## 1. Stripe Dashboard setup

1. [Stripe Dashboard](https://dashboard.stripe.com/test/apikeys) → copy **Secret key** (`sk_test_…`)
2. **Connect** → enable Express accounts (Settings → Connect)
3. **Products** → create recurring prices for platform plans:
   - Starter → copy `price_…` → `EOS_STRIPE_PRICE_STARTER`
   - Pro → copy `price_…` → `EOS_STRIPE_PRICE_PRO`

## 2. Environment (`.env`)

```bash
EOS_SAAS_MODE=true
EOS_SIGNUP_ENABLED=true
EOS_SIGNUP_AUTO_VERIFY_LOCAL=true
EOS_SIGNUP_INVITE_ONLY=false
EOS_BASE_DOMAIN=localhost:8410
EOS_BASE_URL=http://localhost:8410
EOS_BILLING_ENFORCE=true
EOS_COOKIE_SECURE=false

EOS_STRIPE_PLATFORM_SECRET_KEY=sk_test_...
EOS_STRIPE_PLATFORM_WEBHOOK_SECRET=whsec_...   # from stripe listen (step 3)
EOS_STRIPE_PRICE_STARTER=price_...
EOS_STRIPE_PRICE_PRO=price_...

# SaaS tenant client payments use Connect only.
EOS_STRIPE_SECRET_KEY=
EOS_STRIPE_WEBHOOK_SECRET=
```

SaaS mode enforces the billing gate regardless of `EOS_BILLING_ENFORCE`; keeping the value `true`
makes that behavior explicit. Leave the legacy solo/client-payment key
(`EOS_STRIPE_SECRET_KEY`) blank for non-default SaaS studios. It is not a fallback for a tenant
whose Connect account is incomplete. `EOS_SIGNUP_AUTO_VERIFY_LOCAL=true` is only for this isolated
test-mode run; production validation rejects it and requires transactional email.

## 3. Forward webhooks locally

Install [Stripe CLI](https://stripe.com/docs/stripe-cli), then:

```bash
make stripe-listen
# copies whsec_... into your terminal — paste into EOS_STRIPE_PLATFORM_WEBHOOK_SECRET
```

Or manually:

```bash
stripe listen --forward-to http://127.0.0.1:8410/stripe/platform/webhook
```

One endpoint handles:

- `account.updated` — Connect onboarding status
- `checkout.session.completed` (subscription) — platform billing
- `checkout.session.completed` (payment) — client invoice/deposit via Connect

## 4. Dogfood checklist

| Step | URL / action |
|------|----------------|
| Run app | `make run` |
| Platform admin | Log in on apex → `/admin/platform/invites` |
| Signup studio | `/signup` with invite code |
| Connect Stripe | Tenant subdomain → `/admin/stripe/connect` → complete Express onboarding (test mode) |
| Platform plan | `/admin/billing` → Subscribe (test card `4242 4242 4242 4242`) |
| Client payment | Publish booking → book a slot → pay deposit with test card |

After Connect onboarding, return URL auto-refreshes account status (`?thanks=1`).

## 5. Test cards

| Card | Result |
|------|--------|
| `4242 4242 4242 4242` | Success |
| `4000 0000 0000 9995` | Declined |

Use any future expiry, any CVC, any ZIP.

## 6. Verify bound webhooks

Complete the real Eos-created platform subscription and invoice/deposit Checkouts from step 4.
Those sessions contain the exact tenant, invoice, amount, currency, payment-rail, and Connect
destination bindings that Eos verifies. Watch `make run` and the Stripe listener for the resulting
signed `checkout.session.completed` events, then confirm the corresponding subscription/invoice
changed once in Eos.

Do not use a generic `stripe trigger checkout.session.completed` as proof of payment. Its synthetic
session is not bound to an Eos invoice and should be rejected by the hardened handler. Replay the
same captured signed test event through your controlled Stripe test endpoint to confirm the durable
receipt prevents duplicate business effects.
