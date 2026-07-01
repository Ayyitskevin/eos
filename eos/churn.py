"""Rebooking alerts — agents inactive N days."""

import datetime as dt
from typing import Any

from . import db
from .vocab import STUDIO_ID

DEFAULT_INACTIVE_DAYS = 90
HIGH_VALUE_PAID_CENTS = 50_000
HIGH_VALUE_LISTINGS = 4


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


def _days_since(value: str | None) -> int | None:
    if not value:
        return None
    try:
        seen = dt.date.fromisoformat(value[:10])
    except ValueError:
        return None
    return (dt.date.today() - seen).days


def _money(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${dollars:,.0f}"
    return f"${dollars:,.2f}"


def _hydrate(row: Any) -> dict[str, Any]:
    n_listings = int(row["n_listings"] or 0)
    paid_cents = int(row["paid_cents"] or 0)
    days_idle = _days_since(row["last_listing_at"])
    if paid_cents >= HIGH_VALUE_PAID_CENTS or n_listings >= HIGH_VALUE_LISTINGS:
        priority = "High value"
        priority_key = "priority-high"
    elif n_listings:
        priority = "Warm"
        priority_key = "priority-warm"
    else:
        priority = "New"
        priority_key = "priority-new"

    parts: list[str] = []
    if paid_cents:
        parts.append(f"{_money(paid_cents)} paid")
    parts.append(f"{n_listings} listing{'s' if n_listings != 1 else ''}")
    if days_idle is None:
        parts.append("needs first booking")
    else:
        parts.append(f"idle {days_idle} days")

    return {
        "id": row["id"],
        "name": row["name"],
        "company": row["company"],
        "email": row["email"],
        "last_listing_at": row["last_listing_at"],
        "n_listings": n_listings,
        "active_listings": int(row["active_listings"] or 0),
        "paid_cents": paid_cents,
        "paid_display": _money(paid_cents),
        "days_idle": days_idle,
        "priority": priority,
        "priority_key": priority_key,
        "reason_line": " · ".join(parts),
        "next_action": "Start a repeat listing" if row["email"] else "Add email, then rebook",
        "client_href": f"/admin/clients/{row['id']}",
        "book_href": f"/admin/listings/new?client_id={row['id']}",
    }


def _opportunity_rows(
    *,
    days: int = DEFAULT_INACTIVE_DAYS,
    limit: int = 8,
    client_id: int | None = None,
):
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    client_clause = "AND c.id=?" if client_id is not None else ""
    params: list[Any] = [STUDIO_ID]
    if client_id is not None:
        params.append(client_id)
    params.extend([cutoff, limit])
    return db.all_(
        f"""SELECT * FROM (
               SELECT c.id, c.name, c.company, c.email,
                      (SELECT MAX(l.created_at)
                         FROM listings l
                        WHERE l.studio_id=c.studio_id AND l.client_id=c.id) AS last_listing_at,
                      (SELECT COUNT(*)
                         FROM listings l
                        WHERE l.studio_id=c.studio_id AND l.client_id=c.id) AS n_listings,
                      (SELECT COUNT(*)
                         FROM listings l
                        WHERE l.studio_id=c.studio_id
                          AND l.client_id=c.id
                          AND l.status NOT IN ('delivered','archived')) AS active_listings,
                      COALESCE((
                        SELECT SUM(i.amount_cents)
                          FROM invoices i
                         WHERE i.studio_id=c.studio_id
                           AND i.status='paid'
                           AND (
                                i.agent_client_id=c.id
                                OR (i.agent_client_id IS NULL AND i.client_id=c.id)
                           )
                      ), 0) AS paid_cents
                 FROM clients c
                WHERE c.studio_id=? AND c.client_type='agent' {client_clause}
             ) candidates
             WHERE active_listings=0
               AND (last_listing_at IS NULL OR last_listing_at < ?)
             ORDER BY paid_cents DESC, n_listings DESC, COALESCE(last_listing_at, '0000') ASC
             LIMIT ?""",
        tuple(params),
    )


def rebooking_opportunities(*, days: int = DEFAULT_INACTIVE_DAYS, limit: int = 8) -> list[dict]:
    """Rank inactive RE agents by repeat-booking value."""

    return [_hydrate(row) for row in _opportunity_rows(days=days, limit=limit)]


def rebooking_for_client(client_id: int, *, days: int = DEFAULT_INACTIVE_DAYS) -> dict | None:
    rows = _opportunity_rows(days=days, limit=1, client_id=client_id)
    return _hydrate(rows[0]) if rows else None
