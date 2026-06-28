"""Studio profile and service packages."""

from . import db
from .vocab import STUDIO_ID


def get_studio():
    return db.one("SELECT * FROM studio WHERE id=?", (STUDIO_ID,))


def get_profile():
    row = db.one("SELECT * FROM studio_profiles WHERE studio_id=?", (STUDIO_ID,))
    if not row:
        db.run(
            "INSERT INTO studio_profiles (studio_id) VALUES (?)",
            (STUDIO_ID,),
        )
        row = db.one("SELECT * FROM studio_profiles WHERE studio_id=?", (STUDIO_ID,))
    return row


def update_studio(**fields) -> None:
    allowed = {"name", "contact_email", "timezone"}
    parts = []
    params: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        parts.append(f"{k}=?")
        params.append(v.strip() if isinstance(v, str) else v)
    if parts:
        params.append(STUDIO_ID)
        db.run(f"UPDATE studio SET {', '.join(parts)} WHERE id=?", tuple(params))


def update_profile(**fields) -> None:
    allowed = {
        "headline", "about", "service_area", "published",
        "booking_enabled", "min_notice_hours", "buffer_minutes", "slot_minutes",
        "day_start_min", "day_end_min", "book_weekdays",
        "pay_to_download", "watermark_until_paid", "auto_deliver_email", "auto_publish_site",
        "twilight_start_min", "twilight_end_min",
        "delivery_upsell_title", "delivery_upsell_body", "delivery_upsell_link",
    }
    parts = ["updated_at=datetime('now')"]
    params: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        parts.append(f"{k}=?")
        if k == "published":
            params.append(1 if v else 0)
        elif k in ("booking_enabled", "pay_to_download", "watermark_until_paid", "auto_deliver_email", "auto_publish_site"):
            params.append(1 if v else 0)
        else:
            params.append(v)
    params.append(STUDIO_ID)
    db.run(f"UPDATE studio_profiles SET {', '.join(parts)} WHERE studio_id=?", tuple(params))
    db.audit("admin", "studio.profile", None)


def list_packages(*, active_only: bool = False):
    sql = "SELECT * FROM service_packages WHERE studio_id=?"
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY position"
    return db.all_(sql, (STUDIO_ID,))


def list_addons(*, active_only: bool = False):
    sql = "SELECT * FROM service_addons WHERE studio_id=?"
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY position"
    return db.all_(sql, (STUDIO_ID,))


def update_package(
    package_id: int,
    *,
    name: str,
    description: str,
    price_cents: int,
    deposit_cents: int,
    turnaround_hours: int,
    active: bool,
) -> None:
    db.run(
        """UPDATE service_packages SET name=?, description=?, price_cents=?, deposit_cents=?,
           turnaround_hours=?, active=? WHERE id=? AND studio_id=?""",
        (name.strip(), description.strip(), price_cents, deposit_cents,
         turnaround_hours, 1 if active else 0, package_id, STUDIO_ID),
    )


def list_promo_codes():
    return db.all_(
        "SELECT * FROM promo_codes WHERE studio_id=? ORDER BY code",
        (STUDIO_ID,),
    )


def list_crop_presets():
    return db.all_(
        "SELECT * FROM crop_presets WHERE studio_id=? AND active=1 ORDER BY sort",
        (STUDIO_ID,),
    )


def list_inquiries(limit: int = 50):
    return db.all_(
        "SELECT * FROM inquiries WHERE studio_id=? ORDER BY created_at DESC LIMIT ?",
        (STUDIO_ID, limit),
    )


def list_emails(limit: int = 50):
    return db.all_(
        "SELECT * FROM emails_log WHERE studio_id=? ORDER BY sent_at DESC LIMIT ?",
        (STUDIO_ID, limit),
    )


def delivery_upsell() -> dict | None:
    p = get_profile()
    if not (p["delivery_upsell_title"] or p["delivery_upsell_body"]):
        return None
    return {
        "title": p["delivery_upsell_title"] or "Need more for this listing?",
        "body": p["delivery_upsell_body"],
        "link": p["delivery_upsell_link"] or "/book",
    }


def list_activity(limit: int = 100):
    return db.all_(
        "SELECT * FROM audit_log WHERE studio_id=? OR studio_id IS NULL ORDER BY created_at DESC LIMIT ?",
        (STUDIO_ID, limit),
    )