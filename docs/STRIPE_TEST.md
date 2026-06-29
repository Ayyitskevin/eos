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
EOS_BASE_DOMAIN=localhost:8410
EOS_BASE_URL=http://127.0.0.1:8410
EOS_BILLING_ENFORCE=false

EOS_STRIPE_PLATFORM_SECRET_KEY=sk_test_...
EOS_STRIPE_PLATFORM_WEBHOOK_SECRET=whsec_...   # from stripe listen (step 3)
EOS_STRIPE_PRICE_STARTER=price_...
EOS_STRIPE_PRICE_PRO=price_...
```

Legacy solo key (`EOS_STRIPE_SECRET_KEY`) is optional when Connect is active.

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

## 6. Verify webhooks

```bash
stripe trigger checkout.session.completed
```

Watch `make run` logs for `platform webhook` and `invoice … paid via stripe checkout`.