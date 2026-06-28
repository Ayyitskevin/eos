"""Per-studio usage metering for plan enforcement and billing UI."""

import datetime as dt
from pathlib import Path

from . import config, db, plan_limits
from .vocab import STUDIO_ID


def current_period() -> str:
    return dt.date.today().strftime("%Y-%m")


def _ensure_row(period: str | None = None) -> None:
    p = period or current_period()
    db.run(
        """INSERT OR IGNORE INTO studio_usage (studio_id, period)
           VALUES (?,?)""",
        (STUDIO_ID, p),
    )


def bump(field: str, amount: int = 1, *, period: str | None = None) -> None:
    if field not in ("listings_created", "storage_bytes", "api_calls"):
        return
    p = period or current_period()
    _ensure_row(p)
    db.run(
        f"UPDATE studio_usage SET {field}={field}+? WHERE studio_id=? AND period=?",
        (amount, STUDIO_ID, p),
    )


def snapshot(*, period: str | None = None) -> dict:
    p = period or current_period()
    _ensure_row(p)
    row = db.one(
        "SELECT * FROM studio_usage WHERE studio_id=? AND period=?",
        (STUDIO_ID, p),
    )
    limits = plan_limits.limits_for()
    return {
        "period": p,
        "listings_created": row["listings_created"],
        "listings_cap": limits["listings_month"],
        "storage_bytes": row["storage_bytes"],
        "api_calls": row["api_calls"],
        "plan_tier": plan_limits.current_tier(),
    }


def listings_created_this_month() -> int:
    month = dt.date.today().replace(day=1).isoformat()
    row = db.one(
        """SELECT COUNT(*) AS n FROM listings
           WHERE studio_id=? AND created_at >= ?""",
        (STUDIO_ID, month),
    )
    return row["n"] if row else 0


def refresh_storage_bytes() -> int:
    total = 0
    base = config.MEDIA_DIR
    if not base.is_dir():
        bump("storage_bytes", 0)
        return 0
    for path in base.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    row = db.one(
        "SELECT storage_bytes FROM studio_usage WHERE studio_id=? AND period=?",
        (STUDIO_ID, current_period()),
    )
    prev = row["storage_bytes"] if row else 0
    delta = max(0, total - prev)
    if delta:
        bump("storage_bytes", delta)
    elif not row:
        _ensure_row()
        db.run(
            "UPDATE studio_usage SET storage_bytes=? WHERE studio_id=? AND period=?",
            (total, STUDIO_ID, current_period()),
        )
    return total