#!/usr/bin/env bash
# User-level Eos install (no root) — ~/opt/eos + systemd --user
set -euo pipefail

INSTALL_DIR="${EOS_INSTALL_DIR:-$HOME/opt/eos}"
REPO_SRC="$(cd "$(dirname "$0")/.." && pwd)"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "→ Installing Eos to $INSTALL_DIR"

mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
rsync -a --delete \
  --exclude '.venv' --exclude 'data' --exclude 'backups' --exclude '.git' \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.coverage' \
  --exclude '.env' \
  "$REPO_SRC/" "$INSTALL_DIR/"

REQ="$INSTALL_DIR/requirements.txt"
[[ -f "$INSTALL_DIR/requirements-prod.txt" ]] && REQ="$INSTALL_DIR/requirements-prod.txt"

python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q -U pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$REQ"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$INSTALL_DIR/deploy/env.production.example" "$INSTALL_DIR/.env"
  SECRET=$(openssl rand -hex 32)
  ADMIN=$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)
  sed -i "s|change-me-in-production|$SECRET|" "$INSTALL_DIR/.env"
  sed -i "s/change-me-strong-password/$ADMIN/" "$INSTALL_DIR/.env"
  sed -i "s|EOS_DATA_DIR=/opt/eos/data|EOS_DATA_DIR=$INSTALL_DIR/data|" "$INSTALL_DIR/.env"
  echo "→ Wrote $INSTALL_DIR/.env (generated EOS_SECRET_KEY + EOS_ADMIN_PASSWORD)"
fi

mkdir -p "$SYSTEMD_DIR"
sed "s|@INSTALL_DIR@|$INSTALL_DIR|g" "$INSTALL_DIR/deploy/eos-user.service" >"$SYSTEMD_DIR/eos.service"
systemctl --user daemon-reload
systemctl --user enable eos.service

if EOS_CHECK_MODE=production bash -c "set -a && source '$INSTALL_DIR/.env' && set +a && '$INSTALL_DIR/.venv/bin/python' '$INSTALL_DIR/scripts/check-env.py'"; then
  systemctl --user restart eos.service
  echo "→ Eos started: systemctl --user status eos"
else
  echo "WARN: env check failed — edit $INSTALL_DIR/.env then: systemctl --user restart eos"
  exit 1
fi