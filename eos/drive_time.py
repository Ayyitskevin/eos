"""Drive-time aware scheduling — geocode listings, travel buffers between shoots."""

import datetime as dt
import logging
import math

import httpx

from . import db, studio
from .vocab import STUDIO_ID

log = logging.getLogger("eos.drive_time")

_NOMINATIM = "https://nominatim.openstreetmap.org/search"


def _address_key(parts: tuple[str, ...]) -> str:
    return " · ".join(p.strip().lower() for p in parts if p and p.strip())


def geocode_address(
    *,
    line1: str,
    city: str = "",
    state: str = "",
    zip_code: str = "",
) -> tuple[float, float] | None:
    key = _address_key((line1, city, state, zip_code))
    if not key:
        return None
    cached = db.one("SELECT latitude, longitude FROM geocode_cache WHERE address_key=?", (key,))
    if cached:
        return cached["latitude"], cached["longitude"]
    q = ", ".join(p for p in (line1, city, state, zip_code) if p.strip())
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(
                _NOMINATIM,
                params={"q": q, "format": "json", "limit": 1},
                headers={"User-Agent": "Eos-Photography-OS/1.5"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("geocode failed for %s: %s", q, exc)
        return None
    if not data:
        return None
    lat, lng = float(data[0]["lat"]), float(data[0]["lon"])
    db.run(
        "INSERT OR REPLACE INTO geocode_cache (address_key, latitude, longitude) VALUES (?,?,?)",
        (key, lat, lng),
    )
    return lat, lng


def geocode_listing(listing_id: int) -> None:
    row = db.one(
        "SELECT address_line1, city, state, zip FROM listings WHERE id=? AND studio_id=?",
        (listing_id, STUDIO_ID),
    )
    if not row or not row["address_line1"]:
        return
    coords = geocode_address(
        line1=row["address_line1"],
        city=row["city"] or "",
        state=row["state"] or "",
        zip_code=row["zip"] or "",
    )
    if coords:
        db.run(
            "UPDATE listings SET latitude=?, longitude=? WHERE id=? AND studio_id=?",
            (coords[0], coords[1], listing_id, STUDIO_ID),
        )


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def travel_minutes(from_lat: float, from_lng: float, to_lat: float, to_lng: float) -> int:
    """Estimate drive time at ~35 mph average (urban RE photography)."""
    miles = _haversine_miles(from_lat, from_lng, to_lat, to_lng)
    return max(15, int(miles / 35 * 60) + 10)


def travel_ranges_for_day(day: dt.date, buffer_min: int) -> list[tuple[dt.datetime, dt.datetime]]:
    """Extra busy ranges from drive time between confirmed shoots that day."""
    profile = studio.get_profile()
    if not profile["drive_time_enabled"]:
        return []
    day_str = day.isoformat()
    rows = db.all_(
        """SELECT a.starts_at, a.ends_at, l.latitude, l.longitude
           FROM appointments a
           JOIN listings l ON l.id=a.listing_id
           WHERE a.studio_id=? AND a.status IN ('proposed','confirmed')
             AND l.latitude IS NOT NULL AND l.longitude IS NOT NULL
             AND a.starts_at LIKE ?
           ORDER BY a.starts_at""",
        (STUDIO_ID, f"{day_str}%"),
    )
    ranges: list[tuple[dt.datetime, dt.datetime]] = []
    buf = dt.timedelta(minutes=buffer_min)
    for i in range(len(rows) - 1):
        cur, nxt = rows[i], rows[i + 1]
        mins = travel_minutes(cur["latitude"], cur["longitude"], nxt["latitude"], nxt["longitude"])
        end_cur = dt.datetime.strptime(
            (cur["ends_at"] or cur["starts_at"])[:19], "%Y-%m-%d %H:%M:%S"
        )
        start_nxt = dt.datetime.strptime(nxt["starts_at"][:19], "%Y-%m-%d %H:%M:%S")
        travel_end = end_cur + dt.timedelta(minutes=mins)
        if travel_end > start_nxt:
            ranges.append((end_cur - buf, start_nxt + buf))
    return ranges
