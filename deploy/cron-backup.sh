#!/usr/bin/env bash
# Install in crontab (daily 3am):
#   0 3 * * * /opt/eos/deploy/cron-backup.sh >> /var/log/eos-backup.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export EOS_DATA_DIR="${EOS_DATA_DIR:-$ROOT/data}"
DEST="${EOS_BACKUP_DIR:-$ROOT/backups}"

mkdir -p "$DEST"
chmod 700 "$DEST"
"$ROOT/deploy/backup.sh" "$DEST"
find "$DEST" -name 'eos-*.db' -mtime +14 -delete 2>/dev/null || true
find "$DEST" -name 'eos-media-*.tar.gz' -mtime +14 -delete 2>/dev/null || true
find "$DEST" -name 'eos-*.sha256' -mtime +14 -delete 2>/dev/null || true
