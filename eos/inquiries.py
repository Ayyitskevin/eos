"""Booking inquiry helpers."""

from . import db
from .vocab import STUDIO_ID


def list_confirmed(limit: int = 50):
    return db.all_(
        """SELECT * FROM inquiries
           WHERE studio_id=? AND status IN ('confirmed','pending_payment')
           ORDER BY created_at DESC LIMIT ?""",
        (str(STUDIO_ID), limit),
    )
