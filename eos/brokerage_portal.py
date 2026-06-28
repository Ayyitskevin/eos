"""Brokerage self-serve portal — statements and agent deliveries."""

from fastapi import HTTPException

from . import brokerage, clients, db
from .vocab import STUDIO_ID


def get_brokerage_by_token(token: str):
    row = db.one(
        """SELECT * FROM clients
           WHERE portal_token=? AND studio_id=? AND client_type='brokerage'""",
        (token, STUDIO_ID),
    )
    if not row:
        raise HTTPException(status_code=404)
    return row


def agent_deliveries(brokerage_id: int) -> list:
    return db.all_(
        """SELECT l.id, l.title, l.status, l.created_at,
                  c.name AS agent_name,
                  g.slug AS gallery_slug, g.published AS gallery_published
           FROM listings l
           JOIN clients c ON c.id=l.client_id
           LEFT JOIN galleries g ON g.listing_id=l.id AND g.published=1
           WHERE l.studio_id=? AND c.parent_id=?
           ORDER BY l.created_at DESC
           LIMIT 100""",
        (STUDIO_ID, brokerage_id),
    )


def portal_summary(brokerage_id: int) -> dict:
    clients.get_client(brokerage_id)
    totals = brokerage.statement_totals(brokerage_id)
    return {
        "statement_rows": brokerage.statement_rows(brokerage_id)[:50],
        "totals": totals,
        "deliveries": agent_deliveries(brokerage_id),
    }