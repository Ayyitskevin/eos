#!/usr/bin/env bash
set -euo pipefail
umask 077

DATA_DIR="${EOS_DATA_DIR:-./data}"
DEST="${1:-./backups}"
DB_PATH="$DATA_DIR/eos.db"

mkdir -p "$DEST"
chmod 700 "$DEST"
DEST="$(cd "$DEST" && pwd)"
if [[ ! -f "$DB_PATH" ]]; then
  echo "ERROR: Eos database not found: $DB_PATH" >&2
  exit 1
fi
if [[ "$DEST" == *"'"* ]]; then
  echo "ERROR: backup path cannot contain a single quote" >&2
  exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S)-$$"
DB_NAME="eos-$STAMP.db"
MEDIA_NAME="eos-media-$STAMP.tar.gz"
MANIFEST_NAME="eos-$STAMP.sha256"
TMP_DIR="$(mktemp -d "$DEST/.eos-backup-$STAMP.XXXXXX")"
TMP_DB="$TMP_DIR/$DB_NAME"
TMP_MEDIA="$TMP_DIR/$MEDIA_NAME"
TMP_MANIFEST="$TMP_DIR/$MANIFEST_NAME"

cleanup() {
  rm -f "$TMP_DB" "$TMP_MEDIA" "$TMP_MANIFEST"
  rmdir "$TMP_DIR" 2>/dev/null || true
}
trap cleanup EXIT

sqlite3 "$DB_PATH" ".backup '$TMP_DB'"
if [[ "$(sqlite3 "$TMP_DB" "PRAGMA integrity_check;")" != "ok" ]]; then
  echo "ERROR: SQLite backup integrity check failed" >&2
  exit 1
fi

MEDIA_PATHS=()
for path in media brand marketing; do
  [[ -e "$DATA_DIR/$path" ]] && MEDIA_PATHS+=("$path")
done
if (( ${#MEDIA_PATHS[@]} )); then
  tar -czf "$TMP_MEDIA" -C "$DATA_DIR" "${MEDIA_PATHS[@]}"
else
  tar -czf "$TMP_MEDIA" --files-from /dev/null
fi
tar -tzf "$TMP_MEDIA" >/dev/null

mv "$TMP_DB" "$DEST/$DB_NAME"
mv "$TMP_MEDIA" "$DEST/$MEDIA_NAME"
(
  cd "$DEST"
  sha256sum "$DB_NAME" "$MEDIA_NAME"
) >"$TMP_MANIFEST"
mv "$TMP_MANIFEST" "$DEST/$MANIFEST_NAME"
chmod 600 "$DEST/$DB_NAME" "$DEST/$MEDIA_NAME" "$DEST/$MANIFEST_NAME"

echo "verified backup written:"
echo "  $DEST/$DB_NAME"
echo "  $DEST/$MEDIA_NAME"
echo "  $DEST/$MANIFEST_NAME"
