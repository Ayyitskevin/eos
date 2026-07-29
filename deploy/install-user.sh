#!/usr/bin/env bash
# User-level Eos install (no root) — ~/opt/eos + systemd --user
#
# Deploys are release-based: code is staged in $INSTALL_DIR/releases/<git-sha>
# and $INSTALL_DIR/current symlinks to the live release. Rollback:
#   ln -sfn $INSTALL_DIR/releases/<previous-sha> $INSTALL_DIR/current
#   systemctl --user restart eos
# NOTE: the database is forward-only (migrations are append-only, never
# reversed) — rolling the app back across a migration requires restoring the
# database backup that matches the older release.
set -euo pipefail

INSTALL_DIR="${EOS_INSTALL_DIR:-$HOME/opt/eos}"
REPO_SRC="$(cd "$(dirname "$0")/.." && pwd)"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
KEEP_RELEASES=5

echo "→ Installing Eos to $INSTALL_DIR"

RELEASE_ID="$(git -C "$REPO_SRC" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d-%H%M%S)"
RELEASE_DIR="$INSTALL_DIR/releases/$RELEASE_ID"
mkdir -p "$INSTALL_DIR/releases" "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
chmod 700 "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
rsync -a --delete \
  --exclude '.venv' --exclude 'data' --exclude 'backups' --exclude '.git' \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.coverage' \
  --exclude '.env' \
  "$REPO_SRC/" "$RELEASE_DIR/"

REQ="$RELEASE_DIR/requirements.txt"
[[ -f "$RELEASE_DIR/requirements-prod.txt" ]] && REQ="$RELEASE_DIR/requirements-prod.txt"

python3 -m venv "$RELEASE_DIR/.venv"
"$RELEASE_DIR/.venv/bin/pip" install -q -U pip
"$RELEASE_DIR/.venv/bin/pip" install -q -r "$REQ"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$RELEASE_DIR/deploy/env.production.example" "$INSTALL_DIR/.env"
  SECRET=$(openssl rand -hex 32)
  ADMIN=$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)
  sed -i "s|change-me-in-production|$SECRET|" "$INSTALL_DIR/.env"
  sed -i "s/change-me-strong-password/$ADMIN/" "$INSTALL_DIR/.env"
  sed -i "s|EOS_DATA_DIR=/opt/eos/data|EOS_DATA_DIR=$INSTALL_DIR/data|" "$INSTALL_DIR/.env"
  sed -i "s|EOS_BACKUP_DIR=/opt/eos/backups|EOS_BACKUP_DIR=$INSTALL_DIR/backups|" "$INSTALL_DIR/.env"
  echo "→ Wrote $INSTALL_DIR/.env (generated EOS_SECRET_KEY + EOS_ADMIN_PASSWORD)"
fi
chmod 600 "$INSTALL_DIR/.env"
find "$INSTALL_DIR/backups" -maxdepth 1 -type f \
  \( -name 'eos-*.db' -o -name 'eos-media-*.tar.gz' -o -name 'eos-*.sha256' \) \
  -exec chmod 600 {} +

# Flip the symlink only after the release is fully staged.
ln -sfn "$RELEASE_DIR" "$INSTALL_DIR/current"

# Prune old releases, keeping the newest $KEEP_RELEASES.
ls -1dt "$INSTALL_DIR/releases"/*/ | tail -n +$((KEEP_RELEASES + 1)) | xargs -r rm -rf

mkdir -p "$SYSTEMD_DIR"
sed "s|@INSTALL_DIR@|$INSTALL_DIR|g" "$RELEASE_DIR/deploy/eos-user.service" >"$SYSTEMD_DIR/eos.service"
for unit in eos-backup.service eos-backup.timer eos-verify-backup.service eos-verify-backup.timer; do
  sed "s|@INSTALL_DIR@|$INSTALL_DIR|g" "$RELEASE_DIR/deploy/$unit" >"$SYSTEMD_DIR/$unit"
done
systemctl --user daemon-reload
systemctl --user enable eos.service
# Daily backup + weekly restore drill timers (logs: journalctl --user -u eos-backup).
# For timers to run while logged out, enable lingering: loginctl enable-linger "$USER"
systemctl --user enable --now eos-backup.timer eos-verify-backup.timer

if EOS_CHECK_MODE=production bash -c "set -a && source '$INSTALL_DIR/.env' && set +a && '$INSTALL_DIR/current/.venv/bin/python' '$INSTALL_DIR/current/scripts/check-env.py'"; then
  systemctl --user restart eos.service
  echo "→ Eos started: systemctl --user status eos"
  echo "→ Backups: systemctl --user list-timers eos-backup.timer eos-verify-backup.timer"
else
  echo "WARN: env check failed — edit $INSTALL_DIR/.env then: systemctl --user restart eos"
  exit 1
fi
