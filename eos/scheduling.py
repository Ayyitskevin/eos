"""Smart booking slots — weekly hours, buffers, appointment conflicts."""

import datetime as dt

from . import config, db, studio
from .vocab import STUDIO_ID


def _parse_weekdays(raw: str) -> set[int]:
    out: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out or {0, 1, 2, 3, 4, 5}


def _busy_ranges(buffer_min: int) -> list[tuple[dt.datetime, dt.datetime]]:
    rows = db.all_(
        """SELECT starts_at, ends_at FROM appointments
           WHERE studio_id=? AND status IN ('proposed','confirmed')
             AND starts_at IS NOT NULL
             AND starts_at >= datetime('now','-1 day')""",
        (STUDIO_ID,),
    )
    buf = dt.timedelta(minutes=buffer_min)
    ranges: list[tuple[dt.datetime, dt.datetime]] = []
    for r in rows:
        start = dt.datetime.strptime(r["starts_at"][:19], "%Y-%m-%d %H:%M:%S")
        if r["ends_at"]:
            end = dt.datetime.strptime(r["ends_at"][:19], "%Y-%m-%d %H:%M:%S")
        else:
            end = start + dt.timedelta(minutes=90)
        ranges.append((start - buf, end + buf))
    return ranges


def _conflicts(slot_start: dt.datetime, slot_end: dt.datetime, busy: list[tuple[dt.datetime, dt.datetime]]) -> bool:
    for b0, b1 in busy:
        if slot_start < b1 and slot_end > b0:
            return True
    return False


def open_slots(*, days: int = 14) -> list[dict]:
    """Return bookable slots: [{value, label}, ...]."""
    profile = studio.get_profile()
    if not profile["booking_enabled"]:
        return []
    weekdays = _parse_weekdays(profile["book_weekdays"] or "0,1,2,3,4,5")
    slot_min = int(profile["slot_minutes"] or 90)
    buffer_min = int(profile["buffer_minutes"] or 30)
    day_start = int(profile["day_start_min"] or 480)
    day_end = int(profile["day_end_min"] or 1080)
    notice = dt.timedelta(hours=int(profile["min_notice_hours"] or 24))
    now = dt.datetime.now()
    earliest = now + notice
    busy = _busy_ranges(buffer_min)
    slots: list[dict] = []
    for offset in range(days):
        day = (now + dt.timedelta(days=offset)).date()
        if day.weekday() not in weekdays:
            continue
        minute = day_start
        while minute + slot_min <= day_end:
            start = dt.datetime.combine(day, dt.time(hour=minute // 60, minute=minute % 60))
            end = start + dt.timedelta(minutes=slot_min)
            if start >= earliest and not _conflicts(start, end, busy):
                value = start.strftime("%Y-%m-%d %H:%M:%S")
                label = start.strftime("%a %b %d · %I:%M %p").replace(" 0", " ")
                slots.append({"value": value, "label": label})
            minute += slot_min
    return slots


def slot_is_open(starts_at: str) -> bool:
    return any(s["value"] == starts_at for s in open_slots())


def ends_at_for(starts_at: str) -> str:
    profile = studio.get_profile()
    slot_min = int(profile["slot_minutes"] or 90)
    start = dt.datetime.strptime(starts_at[:19], "%Y-%m-%d %H:%M:%S")
    end = start + dt.timedelta(minutes=slot_min)
    return end.strftime("%Y-%m-%d %H:%M:%S")