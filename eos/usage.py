"""Per-studio usage metering for plan enforcement and billing UI."""

import datetime as dt

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
    storage = studio_storage_bytes()
    return {
        "period": p,
        "listings_created": row["listings_created"],
        "listings_cap": limits["listings_month"],
        "storage_bytes": storage,
        "storage_gb": round(storage / (1024**3), 2),
        "storage_cap_gb": limits["storage_gb"],
        "api_calls": row["api_calls"],
        "plan_tier": plan_limits.current_tier(),
        "team_seats": limits["team_seats"],
        "team_count": team_member_count(),
    }


def listings_created_this_month() -> int:
    month = dt.date.today().replace(day=1).isoformat()
    row = db.one(
        """SELECT COUNT(*) AS n FROM listings
           WHERE studio_id=? AND created_at >= ?""",
        (STUDIO_ID, month),
    )
    return row["n"] if row else 0


def studio_storage_bytes(*, studio_id: str | None = None) -> int:
    sid = studio_id or str(STUDIO_ID)
    total = 0
    studio_dir = config.MEDIA_DIR / sid
    if studio_dir.is_dir():
        for path in studio_dir.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
    legacy_dir = config.MEDIA_DIR
    if sid == "default" and legacy_dir.is_dir():
        for path in legacy_dir.iterdir():
            if path.is_dir() and path.name.isdigit():
                for f in path.rglob("*"):
                    if f.is_file():
                        try:
                            total += f.stat().st_size
                        except OSError:
                            pass
    return total


def team_member_count(*, studio_id: str | None = None) -> int:
    sid = studio_id or str(STUDIO_ID)
    row = db.one(
        "SELECT COUNT(*) AS n FROM users WHERE studio_id=? AND active=1",
        (sid,),
    )
    return row["n"] if row else 0


def refresh_storage_bytes() -> int:
    total = studio_storage_bytes()
    _ensure_row()
    db.run(
        "UPDATE studio_usage SET storage_bytes=? WHERE studio_id=? AND period=?",
        (total, STUDIO_ID, current_period()),
    )
    return total


def enforce_storage_limit() -> None:
    plan_limits.check_storage(current_bytes=studio_storage_bytes())
