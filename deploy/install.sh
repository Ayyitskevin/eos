#!/usr/bin/env bash
# Install Eos on a Linux server under /opt/eos (systemd + optional nginx).
set -euo pipefail

INSTALL_DIR="${EOS_INSTALL_DIR:-/opt/eos}"
SERVICE_USER="${EOS_SERVICE_USER:-eos}"
REPO_SRC="$(cd "$(dirname "$0")/.." && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root: sudo deploy/install.sh" >&2
  exit 1
fi

if ! id "$SERVICE_USER" &>/dev/null; then
  useradd -r -m -d "$INSTALL_DIR" -s /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$INSTALL_DIR"
rsync -a --delete \
  --exclude '.venv' --exclude 'data' --exclude '.git' --exclude '__pycache__' \
  "$REPO_SRC/" "$INSTALL_DIR/"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data"

sudo -u "$SERVICE_USER" bash -c "cd '$INSTALL_DIR' && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$INSTALL_DIR/deploy/env.production.example" "$INSTALL_DIR/.env"
  SECRET=$(openssl rand -hex 32)
  sed -i "s/change-me-in-production/$SECRET/" "$INSTALL_DIR/.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
  echo "wrote $INSTALL_DIR/.env — edit EOS_ADMIN_PASSWORD and EOS_BASE_URL before going live"
fi

cp "$INSTALL_DIR/deploy/eos.service" /etc/systemd/system/eos.service
systemctl daemon-reload
systemctl enable eos
systemctl restart eos

echo "Eos installed — systemctl status eos"
echo "Optional: cp $INSTALL_DIR/deploy/nginx-eos.conf /etc/nginx/sites-available/eos && ln -sf ../sites-available/eos /etc/nginx/sites-enabled/"