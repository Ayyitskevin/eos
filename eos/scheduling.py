"""Smart booking slots — weekly hours, buffers, appointment conflicts."""

import datetime as dt
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, db, studio
from .vocab import STUDIO_ID

log = logging.getLogger("eos.scheduling")


class AvailabilityUnavailable(RuntimeError):
    """An external availability source could not be verified."""


def _zone_from_name(name: str | None) -> ZoneInfo:
    """Resolve a deterministic zone, falling back to the operator default then UTC."""
    candidates = (name, config.TIMEZONE, "UTC")
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ZoneInfo(str(candidate))
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            continue
    return ZoneInfo("UTC")


def _studio_zone() -> ZoneInfo:
    row = studio.get_studio()
    configured = row["timezone"] if row else None
    zone = _zone_from_name(configured)
    if configured and zone.key != configured:
        log.error(
            "invalid timezone %r for studio=%s; using %s",
            configured,
            STUDIO_ID,
            zone.key,
        )
    return zone


def _now(zone: ZoneInfo) -> dt.datetime:
    return dt.datetime.now(zone)


def _localize_wall_time(value: dt.datetime, zone: ZoneInfo) -> dt.datetime | None:
    """Return one unambiguous instant for a local wall time, else fail closed."""
    matches: list[dt.datetime] = []
    for fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(dt.UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) == value and round_trip.fold == fold:
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def _parse_weekdays(raw: str) -> set[int]:
    out: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out or {0, 1, 2, 3, 4, 5}


def _busy_ranges(
    buffer_min: int,
    *,
    today: dt.date,
) -> list[tuple[dt.datetime, dt.datetime]]:
    profile = studio.get_profile()
    drive_buf = int(profile["drive_buffer_min"] or 30) if profile["drive_time_enabled"] else 0
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
    try:
        from .integrations import google_calendar

        ranges.extend(google_calendar.busy_ranges())
    except google_calendar.GoogleAvailabilityUnavailable as exc:
        raise AvailabilityUnavailable(str(exc)) from exc
    if drive_buf:
        from . import drive_time

        for offset in range(14):
            day = today + dt.timedelta(days=offset)
            ranges.extend(drive_time.travel_ranges_for_day(day, drive_buf))
    return ranges


def _conflicts(
    slot_start: dt.datetime, slot_end: dt.datetime, busy: list[tuple[dt.datetime, dt.datetime]]
) -> bool:
    for b0, b1 in busy:
        if slot_start < b1 and slot_end > b0:
            return True
    return False


def _slot_list(
    *,
    days: int,
    weekdays: set[int],
    slot_min: int,
    buffer_min: int,
    day_start: int,
    day_end: int,
    label_prefix: str = "",
) -> list[dict]:
    profile = studio.get_profile()
    zone = _studio_zone()
    notice = dt.timedelta(hours=int(profile["min_notice_hours"] or 24))
    now = _now(zone)
    earliest = (now.astimezone(dt.UTC) + notice).astimezone(zone)
    try:
        busy = _busy_ranges(buffer_min, today=now.date())
    except AvailabilityUnavailable:
        log.exception("public booking availability closed for studio=%s", STUDIO_ID)
        return []
    slots: list[dict] = []
    for offset in range(days):
        day = now.date() + dt.timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        minute = day_start
        while minute + slot_min <= day_end:
            start_wall = dt.datetime.combine(
                day,
                dt.time(hour=minute // 60, minute=minute % 60),
            )
            end_wall = start_wall + dt.timedelta(minutes=slot_min)
            start = _localize_wall_time(start_wall, zone)
            end = _localize_wall_time(end_wall, zone)
            expected_duration = dt.timedelta(minutes=slot_min)
            stable_duration = (
                start is not None
                and end is not None
                and end.astimezone(dt.UTC) - start.astimezone(dt.UTC) == expected_duration
            )
            if stable_duration and start >= earliest and not _conflicts(start_wall, end_wall, busy):
                value = start_wall.strftime("%Y-%m-%d %H:%M:%S")
                label = start_wall.strftime("%a %b %d · %I:%M %p").replace(" 0", " ")
                if label_prefix:
                    label = f"{label_prefix}{label}"
                slots.append({"value": value, "label": label})
            minute += slot_min
    return slots


def open_slots(*, days: int = 14) -> list[dict]:
    """Return bookable day slots: [{value, label}, ...]."""
    profile = studio.get_profile()
    if not profile["booking_enabled"]:
        return []
    weekdays = _parse_weekdays(profile["book_weekdays"] or "0,1,2,3,4,5")
    return _slot_list(
        days=days,
        weekdays=weekdays,
        slot_min=int(profile["slot_minutes"] or 90),
        buffer_min=int(profile["buffer_minutes"] or 30),
        day_start=int(profile["day_start_min"] or 480),
        day_end=int(profile["day_end_min"] or 1080),
    )


def twilight_slots(*, days: int = 14) -> list[dict]:
    """Evening twilight window slots for add-on bookings."""
    profile = studio.get_profile()
    if not profile["booking_enabled"]:
        return []
    weekdays = _parse_weekdays(profile["book_weekdays"] or "0,1,2,3,4,5")
    return _slot_list(
        days=days,
        weekdays=weekdays,
        slot_min=int(profile["slot_minutes"] or 90),
        buffer_min=int(profile["buffer_minutes"] or 30),
        day_start=int(profile["twilight_start_min"] or 1020),
        day_end=int(profile["twilight_end_min"] or 1140),
        label_prefix="Twilight · ",
    )


def reschedule_slots(*, days: int = 14) -> list[dict]:
    """Open slots for agent self-reschedule (ignores booking_enabled gate)."""
    profile = studio.get_profile()
    weekdays = _parse_weekdays(profile["book_weekdays"] or "0,1,2,3,4,5")
    return _slot_list(
        days=days,
        weekdays=weekdays,
        slot_min=int(profile["slot_minutes"] or 90),
        buffer_min=int(profile["buffer_minutes"] or 30),
        day_start=int(profile["day_start_min"] or 480),
        day_end=int(profile["day_end_min"] or 1080),
    )


def slot_is_open(starts_at: str, *, twilight: bool = False) -> bool:
    slots = twilight_slots() if twilight else open_slots()
    return any(s["value"] == starts_at for s in slots)


def ends_at_for(starts_at: str) -> str:
    profile = studio.get_profile()
    slot_min = int(profile["slot_minutes"] or 90)
    start = dt.datetime.strptime(starts_at[:19], "%Y-%m-%d %H:%M:%S")
    end = start + dt.timedelta(minutes=slot_min)
    return end.strftime("%Y-%m-%d %H:%M:%S")
