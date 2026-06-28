"""Photographer pay tracking per listing."""

from . import db
from .vocab import STUDIO_ID


def pay_report(*, days: int = 90) -> list:
    return db.all_(
        """SELECT l.id, l.title, l.shoot_date, l.photographer_pay_cents, l.status,
                  u.name AS photographer_name, u.id AS photographer_id
           FROM listings l
           LEFT JOIN users u ON u.id=l.assigned_user_id
           WHERE l.studio_id=? AND l.photographer_pay_cents IS NOT NULL
             AND l.photographer_pay_cents > 0
             AND l.created_at >= datetime('now', ?)
           ORDER BY l.shoot_date DESC, l.id DESC""",
        (STUDIO_ID, f"-{days} days"),
    )


def totals_by_photographer(*, days: int = 90) -> list:
    return db.all_(
        """SELECT u.id, u.name,
                  COUNT(*) AS n_shoots,
                  SUM(l.photographer_pay_cents) AS total_cents
           FROM listings l
           JOIN users u ON u.id=l.assigned_user_id
           WHERE l.studio_id=? AND l.photographer_pay_cents > 0
             AND l.created_at >= datetime('now', ?)
           GROUP BY u.id ORDER BY total_cents DESC""",
        (STUDIO_ID, f"-{days} days"),
    )