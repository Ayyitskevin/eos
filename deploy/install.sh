#!/usr/bin/env bash
# Install Eos on Linux under /opt/eos — systemd + optional Caddy TLS.
#
# Deploys are release-based: code is staged in $INSTALL_DIR/releases/<git-sha>
# and $INSTALL_DIR/current symlinks to the live release. Rollback:
#   ln -sfn $INSTALL_DIR/releases/<previous-sha> $INSTALL_DIR/current
#   systemctl restart eos
# NOTE: the database is forward-only (migrations are append-only, never
# reversed) — rolling the app back across a migration requires restoring the
# database backup that matches the older release.
set -euo pipefail

INSTALL_DIR="${EOS_INSTALL_DIR:-/opt/eos}"
SERVICE_USER="${EOS_SERVICE_USER:-eos}"
INSTALL_CADDY="${INSTALL_CADDY:-0}"
EOS_DOMAIN="${EOS_DOMAIN:-}"
REPO_SRC="$(cd "$(dirname "$0")/.." && pwd)"
KEEP_RELEASES=5
CRON_FILE=/etc/cron.d/eos-backup
LOGROTATE_FILE=/etc/logrotate.d/eos
BACKUP_LOG=/var/log/eos-backup.log

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root: sudo INSTALL_CADDY=1 deploy/install.sh" >&2
  exit 1
fi

if ! id "$SERVICE_USER" &>/dev/null; then
  useradd -r -m -d "$INSTALL_DIR" -s /usr/sbin/nologin "$SERVICE_USER"
fi

RELEASE_ID="$(git -C "$REPO_SRC" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d-%H%M%S)"
RELEASE_DIR="$INSTALL_DIR/releases/$RELEASE_ID"
mkdir -p "$INSTALL_DIR/releases"
rsync -a --delete \
  --exclude '.venv' --exclude 'data' --exclude 'backups' --exclude '.git' --exclude '__pycache__' \
  --exclude '.env' \
  "$REPO_SRC/" "$RELEASE_DIR/"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
chmod 700 "$INSTALL_DIR/data" "$INSTALL_DIR/backups"

REQ="$RELEASE_DIR/requirements.txt"
[[ -f "$RELEASE_DIR/requirements-prod.txt" ]] && REQ="$RELEASE_DIR/requirements-prod.txt"
sudo -u "$SERVICE_USER" bash -c "cd '$RELEASE_DIR' && python3 -m venv .venv && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q -r '$REQ'"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$RELEASE_DIR/deploy/env.production.example" "$INSTALL_DIR/.env"
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

# Flip the symlink only after the release is fully staged.
ln -sfn "$RELEASE_DIR" "$INSTALL_DIR/current"

# Prune old releases, keeping the newest $KEEP_RELEASES.
ls -1dt "$INSTALL_DIR/releases"/*/ | tail -n +$((KEEP_RELEASES + 1)) | xargs -r rm -rf

cp "$RELEASE_DIR/deploy/eos.service" /etc/systemd/system/eos.service
systemctl daemon-reload
systemctl enable eos

# Daily backup (3:00) + weekly restore drill (Sun 4:30), appended to
# $BACKUP_LOG (rotated via deploy/eos.logrotate). App logs live in journald.
touch "$BACKUP_LOG"
chown "$SERVICE_USER:$SERVICE_USER" "$BACKUP_LOG"
cat >"$CRON_FILE" <<EOF
# Eos backups — installed by deploy/install.sh
0 3 * * * $SERVICE_USER $INSTALL_DIR/current/deploy/cron-backup.sh >> $BACKUP_LOG 2>&1
30 4 * * 0 $SERVICE_USER $INSTALL_DIR/current/deploy/cron-verify-backup.sh >> $BACKUP_LOG 2>&1
EOF
chmod 644 "$CRON_FILE"
install -m 0644 "$RELEASE_DIR/deploy/eos.logrotate" "$LOGROTATE_FILE"

if EOS_CHECK_MODE=production sudo -u "$SERVICE_USER" bash -c "set -a && source '$INSTALL_DIR/.env' && set +a && '$INSTALL_DIR/current/.venv/bin/python' '$INSTALL_DIR/current/scripts/check-env.py'" 2>/dev/null; then
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
  sed "s/eos\.example\.com/$EOS_DOMAIN/g" "$INSTALL_DIR/current/deploy/Caddyfile" >"$CADDY_TMP"
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
echo ""
echo "Ops:"
echo "  - Backups: daily via $CRON_FILE -> $INSTALL_DIR/backups (+ offsite S3 when EOS_S3_BUCKET set)"
echo "  - Restore drill: weekly via $CRON_FILE; failures ping EOS_HEALTHCHECK_URL"
echo "  - Rollback: ln -sfn $INSTALL_DIR/releases/<sha> $INSTALL_DIR/current && systemctl restart eos"
echo "    (DB is forward-only — also restore the matching backup when crossing migrations)"
