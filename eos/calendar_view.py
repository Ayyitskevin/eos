"""Admin calendar grid — month/week views, Google busy overlay."""

from __future__ import annotations

import calendar as cal
import datetime as dt

from . import db
from .vocab import STUDIO_ID

_KIND_COLORS = {
    "shoot": "shoot",
    "twilight": "twilight",
    "consultation": "consult",
    "other": "other",
}


def parse_anchor(raw: str | None) -> dt.date:
    if raw:
        try:
            return dt.date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return dt.date.today()


def month_bounds(anchor: dt.date) -> tuple[dt.date, dt.date]:
    first = anchor.replace(day=1)
    if first.month == 12:
        next_month = first.replace(year=first.year + 1, month=1)
    else:
        next_month = first.replace(month=first.month + 1)
    return first, next_month


def week_bounds(anchor: dt.date) -> tuple[dt.date, dt.date]:
    start = anchor - dt.timedelta(days=anchor.weekday())
    return start, start + dt.timedelta(days=7)


def month_grid(year: int, month: int) -> list[list[dt.date | None]]:
    return cal.Calendar(firstweekday=0).monthdayscalendar(year, month)


def week_days(anchor: dt.date) -> list[dt.date]:
    start = anchor - dt.timedelta(days=anchor.weekday())
    return [start + dt.timedelta(days=i) for i in range(7)]


def _event_style(row) -> str:
    if row.get("external_source") == "google":
        return "google"
    return _KIND_COLORS.get(row.get("kind") or "other", "other")


def list_events(
    *,
    range_start: dt.date,
    range_end: dt.date,
    photographer_id: int | None = None,
) -> list[dict]:
    sql = """SELECT a.*, l.title AS listing_title, c.name AS client_name,
                    u.name AS photographer_name
             FROM appointments a
             LEFT JOIN listings l ON l.id=a.listing_id
             LEFT JOIN clients c ON c.id=a.client_id
             LEFT JOIN users u ON u.id=a.assigned_user_id
             WHERE a.studio_id=? AND a.status NOT IN ('canceled','completed')
               AND a.starts_at IS NOT NULL
               AND a.starts_at >= ? AND a.starts_at < ?"""
    params: list = [
        STUDIO_ID,
        range_start.isoformat(),
        range_end.isoformat(),
    ]
    if photographer_id:
        sql += " AND a.assigned_user_id=?"
        params.append(photographer_id)
    sql += " ORDER BY a.starts_at"
    rows = db.all_(sql, tuple(params))
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["style"] = _event_style(d)
        d["is_google"] = d.get("external_source") == "google"
        uid = d.get("assigned_user_id")
        d["photographer_hue"] = (uid or 0) % 5
        d["day"] = d["starts_at"][:10]
        d["time_label"] = d["starts_at"][11:16] if len(d["starts_at"]) >= 16 else ""
        out.append(d)
    return out


def events_by_day(events: list[dict]) -> dict[str, list[dict]]:
    by_day: dict[str, list[dict]] = {}
    for ev in events:
        by_day.setdefault(ev["day"], []).append(ev)
    return by_day


def busy_blocks(
    *,
    range_start: dt.date,
    range_end: dt.date,
) -> list[dict]:
    from .integrations import google_calendar

    blocks: list[dict] = []
    try:
        days = max(1, (range_end - range_start).days + 1)
        for b0, b1 in google_calendar.busy_ranges(days=days):
            if b1.date() < range_start or b0.date() >= range_end:
                continue
            blocks.append({
                "starts_at": b0.strftime("%Y-%m-%d %H:%M:%S"),
                "ends_at": b1.strftime("%Y-%m-%d %H:%M:%S"),
                "day": b0.date().isoformat(),
                "time_label": f"{b0.strftime('%H:%M')}–{b1.strftime('%H:%M')}",
                "title": "Busy (Google Calendar)",
                "style": "busy",
            })
    except Exception:
        pass
    return blocks


def nav_dates(anchor: dt.date, *, view: str) -> dict:
    if view == "week":
        prev_anchor = anchor - dt.timedelta(days=7)
        next_anchor = anchor + dt.timedelta(days=7)
        label = f"Week of {week_days(anchor)[0].strftime('%b %d, %Y')}"
    else:
        if anchor.month == 1:
            prev_anchor = anchor.replace(year=anchor.year - 1, month=12)
        else:
            prev_anchor = anchor.replace(month=anchor.month - 1)
        if anchor.month == 12:
            next_anchor = anchor.replace(year=anchor.year + 1, month=1)
        else:
            next_anchor = anchor.replace(month=anchor.month + 1)
        label = anchor.strftime("%B %Y")
    return {
        "prev": prev_anchor.isoformat(),
        "next": next_anchor.isoformat(),
        "label": label,
    }