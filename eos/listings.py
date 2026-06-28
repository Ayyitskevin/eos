"""Listing spine — RE property fields, shot lists, tasks."""

import datetime as dt

from fastapi import HTTPException

from . import config, db, security
from .vocab import (
    DEFAULT_LISTING_TASKS,
    DEFAULT_SHOT_LIST,
    STUDIO_ID,
)


def format_address(row) -> str:
    parts = [row["address_line1"]]
    if row["address_line2"]:
        parts.append(row["address_line2"])
    city_state = ", ".join(p for p in (row["city"], row["state"]) if p)
    if city_state and row["zip"]:
        city_state = f"{city_state} {row['zip']}"
    elif row["zip"]:
        city_state = row["zip"]
    if city_state:
        parts.append(city_state)
    return " · ".join(p for p in parts if p) or row["title"]


def get_listing(listing_id: int):
    row = db.one("SELECT * FROM listings WHERE id=? AND studio_id=?", (listing_id, STUDIO_ID))
    if not row:
        raise HTTPException(status_code=404)
    return row


def list_listings(*, status: str | None = None):
    sql = """SELECT l.*, c.name AS client_name, c.company AS client_company,
                    (SELECT COUNT(*) FROM listing_shots s WHERE s.listing_id=l.id AND s.done=0) AS shots_open,
                    (SELECT COUNT(*) FROM listing_tasks t WHERE t.listing_id=l.id AND t.done=0) AS tasks_open
             FROM listings l
             LEFT JOIN clients c ON c.id=l.client_id
             WHERE l.studio_id=?"""
    params: list = [STUDIO_ID]
    if status:
        sql += " AND l.status=?"
        params.append(status)
    sql += (
        " ORDER BY CASE l.status"
        " WHEN 'lead' THEN 1 WHEN 'booked' THEN 2 WHEN 'shooting' THEN 3"
        " WHEN 'editing' THEN 4 WHEN 'delivered' THEN 5 ELSE 6 END,"
        " COALESCE(l.due_at, '9999') ASC, l.created_at DESC"
    )
    return db.all_(sql, tuple(params))


def pipeline_counts() -> dict[str, int]:
    rows = db.all_(
        "SELECT status, COUNT(*) AS n FROM listings WHERE studio_id=? GROUP BY status",
        (STUDIO_ID,),
    )
    return {r["status"]: r["n"] for r in rows}


def _default_due_at() -> str:
    due = dt.datetime.now() + dt.timedelta(hours=config.DEFAULT_TURNAROUND_HOURS)
    return due.strftime("%Y-%m-%d %H:%M")


def create_listing(
    title: str,
    *,
    client_id: int | None = None,
    property_type: str = "residential",
    address_line1: str = "",
    address_line2: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
    mls_id: str = "",
    beds: float | None = None,
    baths: float | None = None,
    sqft: int | None = None,
    shoot_date: str | None = None,
    due_at: str | None = None,
    access_notes: str = "",
    notes: str = "",
    seed_defaults: bool = True,
) -> int:
    lid = db.run(
        """INSERT INTO listings
           (studio_id, client_id, title, property_type,
            address_line1, address_line2, city, state, zip, mls_id,
            beds, baths, sqft, shoot_date, due_at, access_notes, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            STUDIO_ID, client_id, title.strip(), property_type,
            address_line1.strip(), address_line2.strip(),
            city.strip(), state.strip(), zip_code.strip(), mls_id.strip(),
            beds, baths, sqft, shoot_date, due_at or _default_due_at(),
            access_notes.strip(), notes.strip(),
        ),
    )
    if seed_defaults:
        for i, (room, shot_title, priority) in enumerate(DEFAULT_SHOT_LIST):
            db.run(
                """INSERT INTO listing_shots
                   (studio_id, listing_id, room, title, priority, position)
                   VALUES (?,?,?,?,?,?)""",
                (STUDIO_ID, lid, room, shot_title, priority, i),
            )
        for label in DEFAULT_LISTING_TASKS:
            db.run(
                "INSERT INTO listing_tasks (studio_id, listing_id, label) VALUES (?,?,?)",
                (STUDIO_ID, lid, label),
            )
    db.audit("admin", "listing.create", f"id={lid} title={title.strip()}")
    return lid


def update_listing(listing_id: int, **fields) -> None:
    allowed = {
        "client_id", "title", "status", "property_type",
        "address_line1", "address_line2", "city", "state", "zip", "mls_id",
        "beds", "baths", "sqft", "shoot_date", "due_at", "access_notes", "notes",
    }
    parts = ["updated_at=datetime('now')"]
    params: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        parts.append(f"{k}=?")
        if isinstance(v, str):
            params.append(v.strip())
        else:
            params.append(v)
    params.append(listing_id)
    db.run(f"UPDATE listings SET {', '.join(parts)} WHERE id=?", tuple(params))
    db.audit("admin", "listing.update", f"id={listing_id}")


def listing_shots(listing_id: int):
    return db.all_(
        "SELECT * FROM listing_shots WHERE listing_id=? ORDER BY position, id",
        (listing_id,),
    )


def listing_tasks(listing_id: int):
    return db.all_(
        "SELECT * FROM listing_tasks WHERE listing_id=? ORDER BY id",
        (listing_id,),
    )


def toggle_shot(shot_id: int, done: bool) -> None:
    db.run("UPDATE listing_shots SET done=? WHERE id=?", (1 if done else 0, shot_id))


def toggle_task(task_id: int, done: bool) -> None:
    db.run("UPDATE listing_tasks SET done=? WHERE id=?", (1 if done else 0, task_id))


def listing_galleries(listing_id: int):
    return db.all_(
        "SELECT * FROM galleries WHERE listing_id=? ORDER BY created_at DESC",
        (listing_id,),
    )