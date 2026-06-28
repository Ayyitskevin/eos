"""Client self-reschedule via portal token."""

import datetime as dt

from fastapi import HTTPException

from . import appointments, db, scheduling, security
from .vocab import STUDIO_ID

_HOLD_MINUTES = 15


def upcoming_for_client(client_id: int) -> list:
    return db.all_(
        """SELECT a.*, l.title AS listing_title
           FROM appointments a
           LEFT JOIN listings l ON l.id=a.listing_id
           WHERE a.studio_id=? AND a.client_id=? AND a.status IN ('proposed','confirmed')
             AND a.external_source IS NULL
             AND a.starts_at >= datetime('now')
           ORDER BY a.starts_at""",
        (STUDIO_ID, client_id),
    )


def create_hold(*, appointment_id: int, client_id: int, starts_at: str) -> str:
    appt = appointments.get_appointment(appointment_id)
    if appt["client_id"] != client_id:
        raise HTTPException(status_code=403)
    if appt.get("external_source") == "google":
        raise HTTPException(status_code=400, detail="Contact studio to reschedule this shoot.")
    twilight = appt["kind"] == "twilight"
    open_vals = {s["value"] for s in scheduling.reschedule_slots()}
    if starts_at not in open_vals:
        raise HTTPException(status_code=409, detail="That slot is no longer available.")
    token = security.new_token()
    expires = (dt.datetime.now() + dt.timedelta(minutes=_HOLD_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    db.run("DELETE FROM appointment_holds WHERE appointment_id=?", (appointment_id,))
    db.run(
        """INSERT INTO appointment_holds
           (studio_id, appointment_id, client_id, starts_at, token, expires_at)
           VALUES (?,?,?,?,?,?)""",
        (STUDIO_ID, appointment_id, client_id, starts_at, token, expires),
    )
    return token


def confirm_hold(token: str, *, client_id: int) -> int:
    row = db.one(
        """SELECT * FROM appointment_holds
           WHERE token=? AND studio_id=? AND client_id=?""",
        (token, STUDIO_ID, client_id),
    )
    if not row:
        raise HTTPException(status_code=404)
    if row["expires_at"] < dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
        raise HTTPException(status_code=410, detail="Hold expired — pick a slot again.")
    appointments.reschedule_appointment(row["appointment_id"], starts_at=row["starts_at"])
    db.run("UPDATE appointments SET status='confirmed' WHERE id=?", (row["appointment_id"],))
    db.run("DELETE FROM appointment_holds WHERE id=?", (row["id"],))
    db.audit("portal", "appointment.reschedule", f"id={row['appointment_id']}")
    return row["appointment_id"]