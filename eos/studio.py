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
    allowed = {"headline", "about", "service_area", "published"}
    parts = ["updated_at=datetime('now')"]
    params: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        parts.append(f"{k}=?")
        params.append(1 if k == "published" and v else (0 if k == "published" else v))
    params.append(STUDIO_ID)
    db.run(f"UPDATE studio_profiles SET {', '.join(parts)} WHERE studio_id=?", tuple(params))
    db.audit("admin", "studio.profile", None)


def list_packages():
    return db.all_(
        "SELECT * FROM service_packages WHERE studio_id=? ORDER BY position",
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


def list_activity(limit: int = 100):
    return db.all_(
        "SELECT * FROM audit_log WHERE studio_id=? OR studio_id IS NULL ORDER BY created_at DESC LIMIT ?",
        (STUDIO_ID, limit),
    )