#!/usr/bin/env bash
# Weekly restore drill of the newest backup set. Scheduled alongside
# cron-backup.sh by deploy/install.sh (/etc/cron.d) or a systemd user timer
# (deploy/install-user.sh). Pings EOS_HEALTHCHECK_URL_VERIFY (fallback:
# EOS_HEALTHCHECK_URL) on success/failure.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

ENV_FILE="${EOS_ENV_FILE:-$ROOT/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

# Default next to the data dir, not under the release dir (releases are pruned).
DEST="${EOS_BACKUP_DIR:-$(dirname "${EOS_DATA_DIR:-$ROOT/data}")/backups}"
HC_URL="${EOS_HEALTHCHECK_URL_VERIFY:-${EOS_HEALTHCHECK_URL:-}}"

_hc_ping() {
  # $1: ok|fail  $2: note
  [[ -n "$HC_URL" ]] || return 0
  local url="$HC_URL"
  [[ "$1" == "fail" ]] && url="${url%/}/fail"
  curl -fsS -m 10 -X POST --data "$2" "$url" >/dev/null 2>&1 || true
}

DB_BACKUP="$(ls -1t "$DEST"/eos-*.db 2>/dev/null || true)"
DB_BACKUP="${DB_BACKUP%%$'\n'*}"
if [[ -z "$DB_BACKUP" ]]; then
  echo "ERROR: no backup found in $DEST" >&2
  _hc_ping fail "eos restore drill skipped: no backup found"
  exit 1
fi
STAMP="$(basename "$DB_BACKUP" | sed 's/^eos-//; s/\.db$//')"

if ! "$ROOT/deploy/verify-backup.sh" \
  "$DEST/eos-$STAMP.db" "$DEST/eos-media-$STAMP.tar.gz" "$DEST/eos-$STAMP.sha256"; then
  _hc_ping fail "eos restore drill failed ($STAMP)"
  exit 1
fi
_hc_ping ok "eos restore drill ok ($STAMP)"
