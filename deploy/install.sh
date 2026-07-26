#!/usr/bin/env bash
# Install Eos on Linux under /opt/eos — systemd + optional Caddy TLS.
set -euo pipefail

INSTALL_DIR="${EOS_INSTALL_DIR:-/opt/eos}"
SERVICE_USER="${EOS_SERVICE_USER:-eos}"
INSTALL_CADDY="${INSTALL_CADDY:-0}"
EOS_DOMAIN="${EOS_DOMAIN:-}"
REPO_SRC="$(cd "$(dirname "$0")/.." && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root: sudo INSTALL_CADDY=1 deploy/install.sh" >&2
  exit 1
fi

if ! id "$SERVICE_USER" &>/dev/null; then
  useradd -r -m -d "$INSTALL_DIR" -s /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$INSTALL_DIR"
rsync -a --delete \
  --exclude '.venv' --exclude 'data' --exclude 'backups' --exclude '.git' --exclude '__pycache__' \
  --exclude '.env' \
  "$REPO_SRC/" "$INSTALL_DIR/"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
chmod 700 "$INSTALL_DIR/data" "$INSTALL_DIR/backups"

REQ="$INSTALL_DIR/requirements.txt"
[[ -f "$INSTALL_DIR/requirements-prod.txt" ]] && REQ="$INSTALL_DIR/requirements-prod.txt"
sudo -u "$SERVICE_USER" bash -c "cd '$INSTALL_DIR' && python3 -m venv .venv && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q -r '$REQ'"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$INSTALL_DIR/deploy/env.production.example" "$INSTALL_DIR/.env"
  SECRET=$(openssl rand -hex 32)
  ADMIN=$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)
  sed -i "s/change-me-in-production/$SECRET/" "$INSTALL_DIR/.env"
  sed -i "s/change-me-strong-password/$ADMIN/" "$INSTALL_DIR/.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
  echo "wrote $INSTALL_DIR/.env with generated secrets — edit domain, Stripe, and platform admin email"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
chmod 600 "$INSTALL_DIR/.env"
find "$INSTALL_DIR/backups" -maxdepth 1 -type f \
  \( -name 'eos-*.db' -o -name 'eos-media-*.tar.gz' -o -name 'eos-*.sha256' \) \
  -exec chmod 600 {} +

cp "$INSTALL_DIR/deploy/eos.service" /etc/systemd/system/eos.service
systemctl daemon-reload
systemctl enable eos

if EOS_CHECK_MODE=production sudo -u "$SERVICE_USER" bash -c "set -a && source '$INSTALL_DIR/.env' && set +a && '$INSTALL_DIR/.venv/bin/python' '$INSTALL_DIR/scripts/check-env.py'" 2>/dev/null; then
  systemctl restart eos
  echo "Eos started — systemctl status eos"
else
  echo "WARN: env check failed — edit $INSTALL_DIR/.env then: systemctl restart eos"
fi

if [[ "$INSTALL_CADDY" == "1" ]]; then
  CADDY_CERT=/etc/eos/tls/fullchain.pem
  CADDY_KEY=/etc/eos/tls/privkey.pem
  if ! command -v caddy &>/dev/null; then
    echo "ERROR: INSTALL_CADDY=1 requires Caddy to be installed" >&2
    exit 1
  fi
  if [[ -z "$EOS_DOMAIN" || ! "$EOS_DOMAIN" =~ ^[A-Za-z0-9.-]+$ || "$EOS_DOMAIN" == *..* ]]; then
    echo "ERROR: set EOS_DOMAIN to the validated apex host (for example eos.example.com)" >&2
    exit 1
  fi
  if [[ ! -r "$CADDY_CERT" || ! -r "$CADDY_KEY" ]]; then
    echo "ERROR: provision an apex + wildcard DNS-01 certificate at $CADDY_CERT and $CADDY_KEY" >&2
    exit 1
  fi
  CERT_NAMES="$(openssl x509 -in "$CADDY_CERT" -noout -ext subjectAltName)"
  if [[ "$CERT_NAMES" != *"DNS:$EOS_DOMAIN"* || "$CERT_NAMES" != *"DNS:*.$EOS_DOMAIN"* ]]; then
    echo "ERROR: Caddy certificate must contain $EOS_DOMAIN and *.$EOS_DOMAIN" >&2
    exit 1
  fi
  CADDY_TMP="$(mktemp /tmp/eos-caddy.XXXXXX)"
  trap 'rm -f "$CADDY_TMP"' EXIT
  sed "s/eos\.example\.com/$EOS_DOMAIN/g" "$INSTALL_DIR/deploy/Caddyfile" >"$CADDY_TMP"
  caddy validate --config "$CADDY_TMP" --adapter caddyfile
  if [[ -f /etc/caddy/Caddyfile ]]; then
    cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%Y%m%d-%H%M%S)"
  fi
  install -m 0644 "$CADDY_TMP" /etc/caddy/Caddyfile
  systemctl enable caddy
  systemctl reload caddy || systemctl restart caddy
  echo "Caddy configured for $EOS_DOMAIN and *.$EOS_DOMAIN"
fi

echo ""
echo "Next steps:"
echo "  1. Edit $INSTALL_DIR/.env (EOS_BASE_URL, EOS_BASE_DOMAIN, Stripe keys)"
echo "  2. Point DNS: A/AAAA eos.yourdomain.com + wildcard *.eos.yourdomain.com"
echo "  3. Provision a DNS-01 wildcard certificate, then rerun with INSTALL_CADDY=1 EOS_DOMAIN=..."
echo "     OR configure nginx from deploy/nginx-eos.conf"
echo "  4. Stripe webhook: POST https://eos.yourdomain.com/stripe/platform/webhook"
echo "  5. Platform admin: /admin/platform/studios (EOS_PLATFORM_ADMIN_EMAILS)"
