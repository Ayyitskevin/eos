"""Twilight / shoot scheduling."""

from fastapi import HTTPException

from . import db, security
from .vocab import STUDIO_ID

KINDS = ("consultation", "shoot", "twilight", "other")
STATUSES = ("proposed", "confirmed", "canceled", "completed")


def get_appointment(appt_id: int):
    row = db.one("SELECT * FROM appointments WHERE id=? AND studio_id=?", (appt_id, STUDIO_ID))
    if not row:
        raise HTTPException(status_code=404)
    return row


def list_upcoming(*, days: int = 14):
    return db.all_(
        """SELECT a.*, l.title AS listing_title, c.name AS client_name,
                  u.name AS photographer_name
           FROM appointments a
           LEFT JOIN listings l ON l.id=a.listing_id
           LEFT JOIN clients c ON c.id=a.client_id
           LEFT JOIN users u ON u.id=a.assigned_user_id
           WHERE a.studio_id=? AND a.status NOT IN ('canceled','completed')
             AND (a.external_source IS NULL OR a.external_source != 'google')
             AND (a.starts_at IS NULL OR a.starts_at >= datetime('now', ?))
           ORDER BY COALESCE(a.starts_at, '9999')""",
        (STUDIO_ID, f"-{days} days"),
    )


def create_appointment(
    title: str,
    *,
    kind: str = "shoot",
    starts_at: str | None = None,
    location: str = "",
    listing_id: int | None = None,
    client_id: int | None = None,
    assigned_user_id: int | None = None,
) -> int:
    aid = db.run(
        """INSERT INTO appointments
           (studio_id, listing_id, client_id, title, kind, starts_at, location, token, assigned_user_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            STUDIO_ID, listing_id, client_id, title.strip(), kind,
            starts_at, location.strip(), security.new_token(), assigned_user_id,
        ),
    )
    db.audit("admin", "appointment.create", f"id={aid}")
    from .integrations import google_calendar
    google_calendar.enqueue_push(aid)
    return aid


def update_appointment(appt_id: int, **fields) -> None:
    allowed = {"title", "kind", "status", "starts_at", "location", "listing_id", "client_id", "assigned_user_id"}
    parts = []
    params: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        parts.append(f"{k}=?")
        params.append(v.strip() if isinstance(v, str) else v)
    if not parts:
        return
    params.extend([appt_id, STUDIO_ID])
    db.run(f"UPDATE appointments SET {', '.join(parts)} WHERE id=? AND studio_id=?", tuple(params))
    db.audit("admin", "appointment.update", f"id={appt_id}")
    from .integrations import google_calendar
    google_calendar.enqueue_push(appt_id)