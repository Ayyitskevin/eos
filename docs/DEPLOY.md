# Deploy Eos (multi-tenant SaaS)

The production Eos install is rootless: the app lives at `~/opt/eos`, runs as a systemd `--user`
service bound to loopback, and is exposed through a Cloudflare Tunnel. The tunnel terminates public
TLS and routes both the apex and the tenant wildcard hostnames, so the server needs no reverse
proxy, no wildcard certificate, and no DNS-01 setup. For a classic VPS install with Caddy/nginx and
your own wildcard certificate, see the appendix.

## Requirements

- Ubuntu 22.04+ or similar Linux (the app install itself needs no root)
- Python 3.12+
- Domain in a Cloudflare DNS zone (e.g. `eos.yourdomain.com` + `*.eos.yourdomain.com`)
- A Cloudflare Tunnel; the production install shares the tunnel on host **flow** (sudo is needed
  only there, to update `/etc/cloudflared/config.yml`)
- Stripe account (Connect + Billing for SaaS)
- S3 or Cloudflare R2 bucket for media (recommended at 10+ studios)

## Rootless install

```bash
git clone https://github.com/Ayyitskevin/eos.git
cd eos
deploy/install-user.sh
```

Installs to `~/opt/eos` (override with `EOS_INSTALL_DIR`), creates a venv with the production
requirements, seeds `~/opt/eos/.env` with a generated `EOS_SECRET_KEY` and `EOS_ADMIN_PASSWORD` on
first run, and installs `deploy/eos-user.service` as the user unit `eos.service`
(`~/.config/systemd/user/eos.service`). The app binds `127.0.0.1:8410`.

Enable lingering so the service starts at boot and survives logout:

```bash
loginctl enable-linger "$USER"
```

## Configure environment

Edit `~/opt/eos/.env` from `deploy/env.production.example`:

| Variable | Purpose |
|----------|---------|
| `EOS_BASE_URL` | Apex HTTPS URL (`https://eos.yourdomain.com`) |
| `EOS_BASE_DOMAIN` | Tenant subdomain base (`eos.yourdomain.com`) |
| `EOS_SAAS_MODE` | `true` for hosted platform |
| `EOS_SIGNUP_ENABLED` | `true` to expose `/signup` |
| `EOS_SIGNUP_AUTO_VERIFY_LOCAL` | Must remain `false`; production signup requires configured email delivery |
| `EOS_BILLING_ENFORCE` | `true` to gate expired tenants — **required launch gate** (see below) |
| `EOS_STRIPE_PLATFORM_SECRET_KEY` | Platform subscription API key |
| `EOS_STRIPE_PLATFORM_WEBHOOK_SECRET` | Signature secret for `/stripe/platform/webhook` |
| `EOS_STRIPE_PRICE_STARTER`, `EOS_STRIPE_PRICE_PRO` | Existing Stripe recurring price IDs |
| `EOS_S3_*` | Object storage for gallery media |
| `EOS_PLATFORM_ADMIN_EMAILS` | Super-admin for `/admin/platform/studios` |
| `EOS_GOOGLE_CLIENT_ID`, `EOS_GOOGLE_CLIENT_SECRET` | Google OAuth client used for configured calendar/operator flows |
| `EOS_GOOGLE_ADMIN_REDIRECT_URI` | Exact HTTPS callback on the configured apex; tenant login completes through a single-use handoff on the initiating tenant Host |

**Launch gate — `EOS_BILLING_ENFORCE=true`.** Required for production launch: expired tenants must
be gated before real customers sign up. As part of the go-live checklist, verify the production
`.env` sets `EOS_BILLING_ENFORCE=true` — a `false` value is a pre-launch placeholder to close, not
an acceptable steady state.

Validate before restart:

```bash
cd ~/opt/eos
set -a && source .env && set +a
EOS_CHECK_MODE=production .venv/bin/python scripts/check-env.py
.venv/bin/python -m compileall -q eos
systemctl --user restart eos
curl -fsS http://127.0.0.1:8410/readyz
```

The production virtualenv intentionally contains runtime dependencies only, so repository tests
run in the local/CI development environment before installation rather than by adding pytest to the
service environment.

Google operator login is shown only when the request is HTTPS, secure cookies are enabled, the
callback Host exactly matches `EOS_BASE_DOMAIN`, and the initiating Host resolves to that active
tenant's canonical subdomain or verified custom domain. Register only the apex callback URL with
Google. The callback claims OAuth state but never creates a session; the initiating browser finishes
the short-lived handoff on the tenant Host, where Eos sets host-only cookies.

## Cloudflare Tunnel

Cloudflare terminates public TLS and forwards the apex and tenant wildcard hostnames to the app
over the tunnel. DNS lives in the Cloudflare zone:

| Record | Value |
|--------|-------|
| `eos.yourdomain.com` | CNAME → `<tunnel-id>.cfargotunnel.com` (proxied) |
| `*.eos.yourdomain.com` | CNAME → `<tunnel-id>.cfargotunnel.com` (proxied) |

Add the Eos ingress rules to the tunnel host's `/etc/cloudflared/config.yml`, then restart
cloudflared. `deploy/cloudflared-eos.example.yml` is a standalone example;
`deploy/cloudflared-flow-merged.yml` is the production config merged into the existing tunnel on
**flow**:

```yaml
ingress:
  - hostname: eos.yourdomain.com
    service: http://<eos-host>:8410
  - hostname: "*.eos.yourdomain.com"
    service: http://<eos-host>:8410
  - service: http_status:404
```

```bash
sudo cp <merged-config> /etc/cloudflared/config.yml   # on the tunnel host (flow)
sudo systemctl restart cloudflared
```

The user unit binds the app to loopback; if cloudflared runs on a different host than Eos, the app
must listen on an address the tunnel host can reach (the checked-in examples use a Tailscale IP or
LAN hostname).

## Stripe webhooks

| Endpoint | Events |
|----------|--------|
| `https://eos.yourdomain.com/stripe/platform/webhook` | `checkout.session.completed`, `customer.subscription.*`, `account.updated` |
| Per-tenant client payments | Same platform webhook (Connect destination charges) |

Create Starter/Pro products in Stripe Dashboard; set `EOS_STRIPE_PRICE_STARTER` and `EOS_STRIPE_PRICE_PRO`.

## Backups

```bash
# Daily cron (see deploy/cron-backup.sh)
0 3 * * * $HOME/opt/eos/deploy/cron-backup.sh >> $HOME/opt/eos/backups/backup.log 2>&1
```

Each run writes an SQLite backup, local-media archive, and SHA-256 manifest. The command fails if
SQLite integrity or archive validation fails; it never reports success after silently dropping
media. With S3 enabled, media is also in object storage.

Run the read-only restore drill regularly and after changing storage:

```bash
$HOME/opt/eos/deploy/verify-backup.sh \
  $HOME/opt/eos/backups/eos-YYYYMMDD-HHMMSS-PID.db \
  $HOME/opt/eos/backups/eos-media-YYYYMMDD-HHMMSS-PID.tar.gz \
  $HOME/opt/eos/backups/eos-YYYYMMDD-HHMMSS-PID.sha256
```

It verifies checksums, rejects unsafe archive paths, restores into an isolated temporary directory,
extracts the media archive, and runs SQLite `integrity_check`; it does not touch live data.

### Live restore (human approval required)

A live restore replaces shared customer state. Obtain owner approval, stop Eos, and take a separate
timestamped copy of the current `~/opt/eos/data` tree before proceeding. Verify the selected backup
with `verify-backup.sh`, restore the database with SQLite's `.restore` into a staging path, extract
media into a separate staging directory, inspect ownership and contents, then replace the live files
only after that review. Restore as the service user, keep the pre-restore copy until acceptance,
start the service, and require `/readyz`, `make smoke`, and a manual tenant/gallery check before
removing rollback data. Never extract an unverified archive directly over `~/opt/eos/data`.

## Health and readiness

Use `/healthz` as a liveness endpoint and `/readyz` for load balancers or deployment gates.
`/readyz` returns HTTP 503 when core dependencies such as SQLite or disk capacity are not ready.

```bash
curl -fsS https://eos.yourdomain.com/healthz | jq .
curl -fsS https://eos.yourdomain.com/readyz | jq .
systemctl --user status eos
journalctl --user -u eos -f
```

## Post-deploy smoke

After restart, verify public and tenant paths from outside the server:

```bash
curl -fsS https://eos.yourdomain.com/readyz | jq .
curl -fsSI https://eos.yourdomain.com/signup
curl -fsSI https://demo.eos.yourdomain.com/
```

For a local checkout or CI runner, use:

```bash
make smoke
make test
make lint
make security
```

## Appendix: VPS install with Caddy/nginx and wildcard TLS

Alternative deployment for a dedicated VPS with a public IP, where the reverse proxy runs on the
same host and you manage your own apex + wildcard certificate instead of using a Cloudflare Tunnel.
Public TLS requires one certificate containing both the apex and wildcard names.

### Install (root)

```bash
git clone https://github.com/Ayyitskevin/eos.git
cd eos
sudo deploy/install.sh
```

Installs to `/opt/eos`, creates the `eos` system user, venv, and loopback-only systemd unit. TLS is
configured separately after DNS-01 certificate issuance; the installer refuses an incomplete
wildcard Caddy setup. Configure `/opt/eos/.env` from the same variable table above, then validate
and restart with the system unit:

```bash
cd /opt/eos
set -a && source .env && set +a
EOS_CHECK_MODE=production .venv/bin/python scripts/check-env.py
.venv/bin/python -m compileall -q eos
sudo systemctl restart eos
curl -fsS http://127.0.0.1:8410/readyz
```

### DNS

| Record | Value |
|--------|-------|
| `eos.yourdomain.com` | Server IP (apex marketing + signup) |
| `*.eos.yourdomain.com` | Server IP (tenant subdomains) |

### Wildcard TLS (DNS-01 required)

First use your DNS provider's ACME DNS-01 client/plugin to issue one certificate whose SANs include
both `eos.yourdomain.com` and `*.eos.yourdomain.com`. Validate the names and expiry before enabling
the proxy. Never put a DNS API token in this repository or the Caddyfile.

The provider-neutral checked-in Caddy template loads externally managed certificate files. Install
the validated certificate and key at `/etc/eos/tls/fullchain.pem` and
`/etc/eos/tls/privkey.pem`, readable by Caddy, then run:

```bash
sudo env INSTALL_CADDY=1 EOS_DOMAIN=eos.yourdomain.com /opt/eos/deploy/install.sh
```

The installer verifies both SANs, validates the rendered Caddyfile, backs up any existing Caddyfile,
and fails instead of hiding a reload error. Renewal remains the certificate issuer's responsibility;
its successful renewal hook must reload Caddy.

To let Caddy issue/renew the wildcard instead, install the matching `caddy-dns` provider module and
replace the file-based `tls` directive as documented by
[Caddy's TLS directive](https://caddyserver.com/docs/caddyfile/directives/tls). For nginx, provision
the same two-name DNS-01 certificate, update the domain/certificate paths in
`deploy/nginx-eos.conf`, run `nginx -t`, and only then enable/reload the site.

Backups, health/readiness, and smoke checks above apply to both installs; substitute `/opt/eos`
paths and `sudo systemctl` / `journalctl -u eos` for the system unit.
