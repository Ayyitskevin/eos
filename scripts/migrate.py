#!/usr/bin/env python3
"""Migration status helper."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eos import config, db  # noqa: E402


def main() -> int:
    config.ensure_dirs()
    before = {r["version"] for r in db.all_("SELECT version FROM schema_migrations")}
    db.migrate()
    after = {r["version"] for r in db.all_("SELECT version FROM schema_migrations")}
    new = sorted(after - before)
    print(f"database: {config.DB_PATH}")
    print(f"applied: {len(after)} migrations")
    if new:
        print(f"newly applied: {', '.join(new)}")
    else:
        print("schema up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
