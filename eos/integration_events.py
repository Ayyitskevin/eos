"""Integration sync audit log — visible in Studio settings."""

from . import db
from .vocab import STUDIO_ID


def log_event(
    provider: str,
    event: str,
    *,
    detail: str = "",
    ok: bool = True,
    studio_id: str | None = None,
) -> None:
    db.run(
        """INSERT INTO integration_events (studio_id, provider, event, detail, ok)
           VALUES (?,?,?,?,?)""",
        (studio_id or str(STUDIO_ID), provider, event, detail[:500], 1 if ok else 0),
    )


def list_recent(*, limit: int = 20, studio_id: str | None = None):
    return db.all_(
        """SELECT * FROM integration_events WHERE studio_id=?
           ORDER BY created_at DESC LIMIT ?""",
        (studio_id or str(STUDIO_ID), limit),
    )


def set_sync_status(
    provider: str,
    *,
    ok: bool,
    error: str = "",
    studio_id: str | None = None,
) -> None:
    sid = studio_id or str(STUDIO_ID)
    if provider == "google":
        if ok:
            db.run(
                """UPDATE studio_profiles SET google_last_sync_at=datetime('now'),
                   google_last_sync_error=NULL WHERE studio_id=?""",
                (sid,),
            )
        else:
            db.run(
                """UPDATE studio_profiles SET google_last_sync_error=? WHERE studio_id=?""",
                (error[:500], sid),
            )
    elif provider == "dropbox":
        if ok:
            db.run(
                """UPDATE studio_profiles SET dropbox_last_scan_at=datetime('now'),
                   dropbox_last_scan_error=NULL WHERE studio_id=?""",
                (sid,),
            )
        else:
            db.run(
                """UPDATE studio_profiles SET dropbox_last_scan_error=? WHERE studio_id=?""",
                (error[:500], sid),
            )
