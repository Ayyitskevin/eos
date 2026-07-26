#!/usr/bin/env bash
# Read-only restore drill for one Eos database/media backup pair.
set -euo pipefail

DB_BACKUP="${1:?usage: verify-backup.sh DB_BACKUP MEDIA_BACKUP [SHA256_MANIFEST]}"
MEDIA_BACKUP="${2:?usage: verify-backup.sh DB_BACKUP MEDIA_BACKUP [SHA256_MANIFEST]}"
MANIFEST="${3:-${DB_BACKUP%.db}.sha256}"

for path in "$DB_BACKUP" "$MEDIA_BACKUP" "$MANIFEST"; do
  if [[ ! -f "$path" ]]; then
    echo "ERROR: backup artifact not found: $path" >&2
    exit 1
  fi
done

MANIFEST_DIR="$(cd "$(dirname "$MANIFEST")" && pwd)"
(
  cd "$MANIFEST_DIR"
  sha256sum -c "$(basename "$MANIFEST")"
)

if ! tar -tzf "$MEDIA_BACKUP" | awk '
  /^\// { bad=1 }
  /(^|\/)\.\.($|\/)/ { bad=1 }
  END { exit bad ? 1 : 0 }
'; then
  echo "ERROR: media archive is corrupt or contains an unsafe path" >&2
  exit 1
fi

TMP_DIR="$(mktemp -d /tmp/eos-restore-drill.XXXXXX)"
RESTORED_DB="$TMP_DIR/eos.db"
RESTORED_MEDIA="$TMP_DIR/media"
cleanup() {
  find "$TMP_DIR" -mindepth 1 -delete 2>/dev/null || true
  rmdir "$TMP_DIR" 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "$RESTORED_MEDIA"
sqlite3 "$DB_BACKUP" ".backup '$RESTORED_DB'"
tar -xzf "$MEDIA_BACKUP" -C "$RESTORED_MEDIA"
if [[ "$(sqlite3 "$RESTORED_DB" "PRAGMA integrity_check;")" != "ok" ]]; then
  echo "ERROR: restored SQLite database failed integrity_check" >&2
  exit 1
fi

echo "restore drill passed: checksums, archive extraction, and SQLite integrity are valid"
