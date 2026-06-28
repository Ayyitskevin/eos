#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${EOS_DATA_DIR:-./data}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="${1:-./backups}"

mkdir -p "$DEST"
sqlite3 "$DATA_DIR/eos.db" ".backup '$DEST/eos-$STAMP.db'"
tar -czf "$DEST/eos-media-$STAMP.tar.gz" -C "$DATA_DIR" media brand marketing 2>/dev/null || true
echo "backup written to $DEST (eos-$STAMP.db)"