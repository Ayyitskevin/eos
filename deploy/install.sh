#!/usr/bin/env bash
# Install Eos on Linux under /opt/eos — systemd + optional Caddy TLS.
set -euo pipefail

INSTALL_DIR="${EOS_INSTALL_DIR:-/opt/eos}"
SERVICE_USER="${EOS_SERVICE_USER:-eos}"
INSTALL_CADDY="${INSTALL_CADDY:-0}"
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
  "$REPO_SRC/" "$INSTALL_DIR/"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data" "$INSTALL_DIR/backups"

REQ="$INSTALL_DIR/requirements.txt"
[[ -f "$INSTALL_DIR/requirements-prod.txt" ]] && REQ="$INSTALL_DIR/requirements-prod.txt"
sudo -u "$SERVICE_USER" bash -c "cd '$INSTALL_DIR' && python3 -m venv .venv && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q -r '$REQ'"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$INSTALL_DIR/deploy/env.production.example" "$INSTALL_DIR/.env"
  SECRET=$(openssl rand -hex 32)
  sed -i "s/change-me-in-production/$SECRET/" "$INSTALL_DIR/.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
  echo "wrote $INSTALL_DIR/.env — edit domain, Stripe, and platform admin email"
fi

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
  if command -v caddy &>/dev/null; then
    cp "$INSTALL_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
    systemctl enable caddy 2>/dev/null || true
    systemctl reload caddy 2>/dev/null || systemctl restart caddy 2>/dev/null || true
    echo "Caddy configured — edit /etc/caddy/Caddyfile with your domain"
  else
    echo "Caddy not installed — see docs/DEPLOY.md or: INSTALL_CADDY=1 after installing caddy"
  fi
fi

echo ""
echo "Next steps:"
echo "  1. Edit $INSTALL_DIR/.env (EOS_BASE_URL, EOS_BASE_DOMAIN, Stripe keys)"
echo "  2. Point DNS: A/AAAA eos.yourdomain.com + wildcard *.eos.yourdomain.com"
echo "  3. INSTALL_CADDY=1 deploy/install.sh  OR  configure nginx from deploy/nginx-eos.conf"
echo "  4. Stripe webhook: POST https://eos.yourdomain.com/stripe/platform/webhook"
echo "  5. Platform admin: /admin/platform/studios (EOS_PLATFORM_ADMIN_EMAILS)"