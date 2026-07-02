"""Ops reporting — revenue, bookings, AR, top agents."""

import datetime as dt
from typing import Any

from . import db
from .vocab import STUDIO_ID

_OVERDUE_DAYS = 14


def _month_start(d: dt.date | None = None) -> str:
    d = d or dt.date.today()
    return d.replace(day=1).isoformat()


def _year_start(d: dt.date | None = None) -> str:
    d = d or dt.date.today()
    return d.replace(month=1, day=1).isoformat()


def summary() -> dict:
    month = _month_start()
    year = _year_start()
    overdue_cutoff = (dt.date.today() - dt.timedelta(days=_OVERDUE_DAYS)).isoformat()

    rev_mtd = db.one(
        """SELECT COALESCE(SUM(amount_cents),0) AS cents, COUNT(*) AS n
           FROM invoices WHERE studio_id=? AND status='paid'
             AND paid_at >= ?""",
        (STUDIO_ID, month),
    )
    rev_ytd = db.one(
        """SELECT COALESCE(SUM(amount_cents),0) AS cents, COUNT(*) AS n
           FROM invoices WHERE studio_id=? AND status='paid'
             AND paid_at >= ?""",
        (STUDIO_ID, year),
    )
    bookings_mtd = db.one(
        """SELECT COUNT(*) AS n FROM inquiries
           WHERE studio_id=? AND status='confirmed'
             AND created_at >= ?""",
        (STUDIO_ID, month),
    )
    listings_booked_mtd = db.one(
        """SELECT COUNT(*) AS n FROM listings
           WHERE studio_id=? AND status IN ('booked','shooting','editing','delivered')
             AND updated_at >= ?""",
        (STUDIO_ID, month),
    )
    overdue = db.one(
        """SELECT COALESCE(SUM(amount_cents),0) AS cents, COUNT(*) AS n
           FROM invoices WHERE studio_id=? AND status='sent'
             AND created_at < ?""",
        (STUDIO_ID, overdue_cutoff),
    )
    open_ar = db.one(
        """SELECT COALESCE(SUM(amount_cents),0) AS cents, COUNT(*) AS n
           FROM invoices WHERE studio_id=? AND status='sent'""",
        (STUDIO_ID,),
    )

    paid_n = rev_ytd["n"] or 0
    aov_cents = (rev_ytd["cents"] // paid_n) if paid_n else 0

    return {
        "revenue_mtd_cents": rev_mtd["cents"],
        "revenue_mtd_count": rev_mtd["n"],
        "revenue_ytd_cents": rev_ytd["cents"],
        "revenue_ytd_count": rev_ytd["n"],
        "bookings_mtd": (bookings_mtd["n"] or 0) + (listings_booked_mtd["n"] or 0),
        "aov_cents": aov_cents,
        "overdue_ar_cents": overdue["cents"],
        "overdue_ar_count": overdue["n"],
        "open_ar_cents": open_ar["cents"],
        "open_ar_count": open_ar["n"],
    }


def top_agents(*, limit: int = 10) -> list:
    return db.all_(
        """SELECT c.id, c.name, c.company,
                  COUNT(DISTINCT l.id) AS n_listings,
                  COALESCE(SUM(CASE WHEN i.status='paid' THEN i.amount_cents ELSE 0 END),0) AS revenue_cents,
                  COALESCE(SUM(CASE WHEN i.status='sent' THEN i.amount_cents ELSE 0 END),0) AS open_cents
           FROM clients c
           LEFT JOIN listings l ON l.client_id=c.id AND l.studio_id=c.studio_id
           LEFT JOIN invoices i ON i.listing_id=l.id AND i.studio_id=c.studio_id
           WHERE c.studio_id=? AND c.client_type='agent'
           GROUP BY c.id
           ORDER BY revenue_cents DESC, n_listings DESC
           LIMIT ?""",
        (STUDIO_ID, limit),
    )


def _money(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${dollars:,.0f}"
    return f"${dollars:,.2f}"


def _hydrate_repeat_agent(row: Any) -> dict:
    paid_cents = int(row["paid_cents"] or 0)
    open_cents = int(row["open_cents"] or 0)
    n_listings = int(row["n_listings"] or 0)
    n_paid_listings = int(row["n_paid_listings"] or 0)
    return {
        "id": row["id"],
        "name": row["name"],
        "company": row["company"],
        "brokerage_name": row["brokerage_name"],
        "n_listings": n_listings,
        "n_paid_listings": n_paid_listings,
        "paid_cents": paid_cents,
        "open_cents": open_cents,
        "paid_display": _money(paid_cents),
        "open_display": _money(open_cents),
        "last_listing_at": row["last_listing_at"],
        "client_href": f"/admin/clients/{row['id']}",
        "avg_listing_value_cents": paid_cents // n_paid_listings if n_paid_listings else 0,
        "avg_listing_value_display": _money(paid_cents // n_paid_listings)
        if n_paid_listings
        else "$0",
    }


def repeat_agent_revenue(*, min_listings: int = 2, limit: int = 12) -> list[dict]:
    """Agents with repeat listing volume, paid value, and brokerage attribution."""

    rows = db.all_(
        """SELECT * FROM (
             SELECT c.id, c.name, c.company,
                    parent.name AS brokerage_name,
                    (SELECT COUNT(*)
                       FROM listings l
                      WHERE l.studio_id=c.studio_id AND l.client_id=c.id) AS n_listings,
                    (SELECT MAX(l.created_at)
                       FROM listings l
                      WHERE l.studio_id=c.studio_id AND l.client_id=c.id) AS last_listing_at,
                    (SELECT COUNT(DISTINCT i.listing_id)
                       FROM invoices i
                       LEFT JOIN listings li
                         ON li.id=i.listing_id AND li.studio_id=i.studio_id
                      WHERE i.studio_id=c.studio_id
                        AND i.status='paid'
                        AND (
                             i.agent_client_id=c.id
                             OR (i.agent_client_id IS NULL AND i.client_id=c.id)
                             OR (i.agent_client_id IS NULL AND i.client_id IS NULL AND li.client_id=c.id)
                        )) AS n_paid_listings,
                    COALESCE((SELECT SUM(i.amount_cents)
                       FROM invoices i
                       LEFT JOIN listings li
                         ON li.id=i.listing_id AND li.studio_id=i.studio_id
                      WHERE i.studio_id=c.studio_id
                        AND i.status='paid'
                        AND (
                             i.agent_client_id=c.id
                             OR (i.agent_client_id IS NULL AND i.client_id=c.id)
                             OR (i.agent_client_id IS NULL AND i.client_id IS NULL AND li.client_id=c.id)
                        )), 0) AS paid_cents,
                    COALESCE((SELECT SUM(i.amount_cents)
                       FROM invoices i
                       LEFT JOIN listings li
                         ON li.id=i.listing_id AND li.studio_id=i.studio_id
                      WHERE i.studio_id=c.studio_id
                        AND i.status='sent'
                        AND (
                             i.agent_client_id=c.id
                             OR (i.agent_client_id IS NULL AND i.client_id=c.id)
                             OR (i.agent_client_id IS NULL AND i.client_id IS NULL AND li.client_id=c.id)
                        )), 0) AS open_cents
               FROM clients c
               LEFT JOIN clients parent
                 ON parent.id=c.parent_id
                AND parent.studio_id=c.studio_id
                AND parent.client_type='brokerage'
              WHERE c.studio_id=? AND c.client_type='agent'
           ) agents
           WHERE n_listings >= ?
           ORDER BY paid_cents DESC, n_listings DESC, last_listing_at DESC
           LIMIT ?""",
        (STUDIO_ID, min_listings, limit),
    )
    return [_hydrate_repeat_agent(row) for row in rows]


def repeat_agent_summary(*, min_listings: int = 2) -> dict:
    rows = repeat_agent_revenue(min_listings=min_listings, limit=100)
    paid_cents = sum(r["paid_cents"] for r in rows)
    paid_listings = sum(r["n_paid_listings"] for r in rows)
    return {
        "repeat_agent_count": len(rows),
        "paid_cents": paid_cents,
        "paid_display": _money(paid_cents),
        "paid_listing_count": paid_listings,
        "avg_listing_value_cents": paid_cents // paid_listings if paid_listings else 0,
        "avg_listing_value_display": _money(paid_cents // paid_listings) if paid_listings else "$0",
    }


def overdue_invoices() -> list:
    cutoff = (dt.date.today() - dt.timedelta(days=_OVERDUE_DAYS)).isoformat()
    return db.all_(
        """SELECT i.*, l.title AS listing_title, c.name AS client_name
           FROM invoices i
           LEFT JOIN listings l ON l.id=i.listing_id
           LEFT JOIN clients c ON c.id=COALESCE(i.bill_to_client_id, i.client_id)
           WHERE i.studio_id=? AND i.status='sent' AND i.created_at < ?
           ORDER BY i.created_at""",
        (STUDIO_ID, cutoff),
    )


def brokerages_with_balance() -> list:
    return db.all_(
        """SELECT c.id, c.name, c.company,
                  COUNT(i.id) AS n_invoices,
                  COALESCE(SUM(i.amount_cents),0) AS open_cents
           FROM clients c
           JOIN invoices i ON i.bill_to_client_id=c.id AND i.studio_id=c.studio_id
           WHERE c.studio_id=? AND c.client_type='brokerage' AND i.status='sent'
           GROUP BY c.id
           HAVING open_cents > 0
           ORDER BY open_cents DESC""",
        (STUDIO_ID,),
    )
