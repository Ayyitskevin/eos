# Deploy Eos (multi-tenant SaaS)

Eos runs as a single Linux service behind Caddy or nginx with automatic tenant routing via wildcard DNS.

## Requirements

- Ubuntu 22.04+ or similar Linux
- Python 3.12+
- Domain with DNS control (e.g. `eos.yourdomain.com` + `*.eos.yourdomain.com`)
- [Caddy](https://caddyserver.com/) (recommended) or nginx
- Stripe account (Connect + Billing for SaaS)
- S3 or Cloudflare R2 bucket for media (recommended at 10+ studios)

## One-command install

```bash
git clone https://github.com/Ayyitskevin/eos.git
cd eos
sudo INSTALL_CADDY=1 deploy/install.sh
```

Installs to `/opt/eos`, creates `eos` system user, venv with prod deps, systemd unit, optional Caddy config.

## Configure environment

Edit `/opt/eos/.env` from `deploy/env.production.example`:

| Variable | Purpose |
|----------|---------|
| `EOS_BASE_URL` | Apex HTTPS URL (`https://eos.yourdomain.com`) |
| `EOS_BASE_DOMAIN` | Tenant subdomain base (`eos.yourdomain.com`) |
| `EOS_SAAS_MODE` | `true` for hosted platform |
| `EOS_SIGNUP_ENABLED` | `true` to expose `/signup` |
| `EOS_BILLING_ENFORCE` | `true` to gate expired tenants |
| `EOS_STRIPE_PLATFORM_*` | Platform subscription billing |
| `EOS_S3_*` | Object storage for gallery media |
| `EOS_PLATFORM_ADMIN_EMAILS` | Super-admin for `/admin/platform/studios` |

Validate:

```bash
cd /opt/eos && EOS_CHECK_MODE=production set -a && source .env && set +a && .venv/bin/python scripts/check-env.py
sudo systemctl restart eos
```

## DNS

| Record | Value |
|--------|-------|
| `eos.yourdomain.com` | Server IP (apex marketing + signup) |
| `*.eos.yourdomain.com` | Server IP (tenant subdomains) |

## Caddy (wildcard TLS)

Edit `deploy/Caddyfile` — replace `eos.example.com` with your domain — then:

```bash
sudo cp /opt/eos/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## Stripe webhooks

| Endpoint | Events |
|----------|--------|
| `https://eos.yourdomain.com/stripe/platform/webhook` | `checkout.session.completed`, `customer.subscription.*`, `account.updated` |
| Per-tenant client payments | Same platform webhook (Connect destination charges) |

Create Starter/Pro products in Stripe Dashboard; set `EOS_STRIPE_PRICE_STARTER` and `EOS_STRIPE_PRICE_PRO`.

## Backups

```bash
# Daily cron (see deploy/cron-backup.sh)
0 3 * * * /opt/eos/deploy/cron-backup.sh >> /var/log/eos-backup.log 2>&1
```

Backs up SQLite DB + local media tarball. With S3 enabled, media is also in object storage.

## Health

```bash
curl -s https://eos.yourdomain.com/healthz | jq .
systemctl status eos
journalctl -u eos -f
```