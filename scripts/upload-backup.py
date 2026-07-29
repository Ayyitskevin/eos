#!/usr/bin/env python3
"""Upload backup artifacts to S3/R2 as an optional offsite copy.

Exits 0 (with a warning) when EOS_S3_BUCKET is unset — local-only backups.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eos import object_store  # noqa: E402


def main() -> int:
    paths = [Path(arg) for arg in sys.argv[1:]]
    if not paths:
        print("usage: upload-backup.py ARTIFACT [ARTIFACT...]", file=sys.stderr)
        return 2
    if not object_store.enabled():
        print("WARN: EOS_S3_BUCKET not set — skipping offsite backup upload (local-only)")
        return 0
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(f"ERROR: backup artifact not found: {', '.join(missing)}", file=sys.stderr)
        return 1
    failed = []
    for path in paths:
        key = object_store.backup_key(path.name)
        if object_store.upload_file(path, key):
            print(f"offsite copy: s3://{object_store.config.S3_BUCKET}/{key}")
        else:
            failed.append(path.name)
    if failed:
        print(f"ERROR: offsite upload failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
