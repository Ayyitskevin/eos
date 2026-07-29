#!/usr/bin/env bash
# Daily Eos backup. Scheduled by deploy/install.sh (/etc/cron.d, root installs)
# or by a systemd user timer (deploy/install-user.sh, rootless installs).
#
# Pings EOS_HEALTHCHECK_URL_BACKUP (fallback: EOS_HEALTHCHECK_URL) on
# success/failure — healthchecks.io-style dead-man switch. Uploads an offsite
# copy to S3/R2 when EOS_S3_BUCKET is set (retention there is governed by
# bucket lifecycle rules; local retention is 14 days).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Cron/timer environments are sparse — load deployment env (S3, healthcheck).
ENV_FILE="${EOS_ENV_FILE:-$ROOT/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

export EOS_DATA_DIR="${EOS_DATA_DIR:-$ROOT/data}"
# Default next to the data dir, not under the release dir (releases are pruned).
DEST="${EOS_BACKUP_DIR:-$(dirname "$EOS_DATA_DIR")/backups}"
HC_URL="${EOS_HEALTHCHECK_URL_BACKUP:-${EOS_HEALTHCHECK_URL:-}}"

_hc_ping() {
  # $1: ok|fail  $2: note — never let the ping itself break the backup run
  [[ -n "$HC_URL" ]] || return 0
  local url="$HC_URL"
  [[ "$1" == "fail" ]] && url="${url%/}/fail"
  curl -fsS -m 10 -X POST --data "$2" "$url" >/dev/null 2>&1 || true
}

mkdir -p "$DEST"
chmod 700 "$DEST"

if ! "$ROOT/deploy/backup.sh" "$DEST"; then
  _hc_ping fail "eos backup failed"
  exit 1
fi

# Offsite copy of the artifact set just written (optional).
DB_LATEST="$(ls -1t "$DEST"/eos-*.db)"
DB_LATEST="${DB_LATEST%%$'\n'*}"
STAMP="$(basename "$DB_LATEST" | sed 's/^eos-//; s/\.db$//')"
if [[ -n "${EOS_S3_BUCKET:-}" ]]; then
  if ! "$ROOT/.venv/bin/python" "$ROOT/scripts/upload-backup.py" \
    "$DEST/eos-$STAMP.db" "$DEST/eos-media-$STAMP.tar.gz" "$DEST/eos-$STAMP.sha256"; then
    _hc_ping fail "eos offsite backup upload failed ($STAMP)"
    exit 1
  fi
else
  echo "WARN: EOS_S3_BUCKET not set — backups are local-only" >&2
fi

find "$DEST" -name 'eos-*.db' -mtime +14 -delete 2>/dev/null || true
find "$DEST" -name 'eos-media-*.tar.gz' -mtime +14 -delete 2>/dev/null || true
find "$DEST" -name 'eos-*.sha256' -mtime +14 -delete 2>/dev/null || true

_hc_ping ok "eos backup ok ($STAMP)"
