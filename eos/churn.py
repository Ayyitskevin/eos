"""Rebooking alerts — agents inactive N days."""

import datetime as dt

from . import db
from .vocab import STUDIO_ID

DEFAULT_INACTIVE_DAYS = 90


def inactive_agents(*, days: int = DEFAULT_INACTIVE_DAYS, limit: int = 15) -> list:
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return db.all_(
        """SELECT c.id, c.name, c.company, c.email,
                  MAX(l.created_at) AS last_listing_at,
                  COUNT(l.id) AS n_listings
           FROM clients c
           LEFT JOIN listings l ON l.client_id=c.id AND l.studio_id=c.studio_id
           WHERE c.studio_id=? AND c.client_type='agent'
           GROUP BY c.id
           HAVING MAX(l.created_at) IS NULL OR MAX(l.created_at) < ?
           ORDER BY COALESCE(MAX(l.created_at), '0000') ASC
           LIMIT ?""",
        (STUDIO_ID, cutoff, limit),
    )
