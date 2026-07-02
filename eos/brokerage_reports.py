"""Brokerage account reporting for owner-side relationship management."""

import csv
import io
from typing import Any

from . import db
from .vocab import STUDIO_ID


def _money(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${dollars:,.0f}"
    return f"${dollars:,.2f}"


def _invoice_matches_brokerage_clause() -> str:
    return """(
        i.bill_to_client_id=c.id
        OR i.agent_client_id IN (
            SELECT a.id FROM clients a
             WHERE a.studio_id=c.studio_id
               AND a.parent_id=c.id
               AND a.client_type='agent'
        )
        OR (
            i.agent_client_id IS NULL
            AND i.client_id IN (
                SELECT a.id FROM clients a
                 WHERE a.studio_id=c.studio_id
                   AND a.parent_id=c.id
                   AND a.client_type='agent'
            )
        )
        OR (
            i.agent_client_id IS NULL
            AND i.client_id IS NULL
            AND li.client_id IN (
                SELECT a.id FROM clients a
                 WHERE a.studio_id=c.studio_id
                   AND a.parent_id=c.id
                   AND a.client_type='agent'
            )
        )
    )"""


def _invoice_matches_agent_clause() -> str:
    return """(
        i.agent_client_id=a.id
        OR (i.agent_client_id IS NULL AND i.client_id=a.id)
        OR (i.agent_client_id IS NULL AND i.client_id IS NULL AND li.client_id=a.id)
    )"""


def top_agents_for_brokerage(brokerage_id: int, *, limit: int = 3) -> list[dict]:
    rows = db.all_(
        f"""SELECT a.id, a.name, a.company,
                   (SELECT COUNT(*)
                      FROM listings l
                     WHERE l.studio_id=a.studio_id AND l.client_id=a.id) AS n_listings,
                   (SELECT MAX(l.created_at)
                      FROM listings l
                     WHERE l.studio_id=a.studio_id AND l.client_id=a.id) AS last_listing_at,
                   COALESCE((SELECT SUM(i.amount_cents)
                      FROM invoices i
                      LEFT JOIN listings li
                        ON li.id=i.listing_id AND li.studio_id=i.studio_id
                     WHERE i.studio_id=a.studio_id
                       AND i.status='paid'
                       AND {_invoice_matches_agent_clause()}), 0) AS paid_cents,
                   COALESCE((SELECT SUM(i.amount_cents)
                      FROM invoices i
                      LEFT JOIN listings li
                        ON li.id=i.listing_id AND li.studio_id=i.studio_id
                     WHERE i.studio_id=a.studio_id
                       AND i.status='sent'
                       AND {_invoice_matches_agent_clause()}), 0) AS open_cents
              FROM clients a
             WHERE a.studio_id=? AND a.parent_id=? AND a.client_type='agent'
             ORDER BY paid_cents DESC, open_cents DESC, n_listings DESC, last_listing_at DESC
             LIMIT ?""",
        (STUDIO_ID, brokerage_id, limit),
    )
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "company": row["company"],
            "n_listings": int(row["n_listings"] or 0),
            "last_listing_at": row["last_listing_at"],
            "paid_cents": int(row["paid_cents"] or 0),
            "open_cents": int(row["open_cents"] or 0),
            "paid_display": _money(int(row["paid_cents"] or 0)),
            "open_display": _money(int(row["open_cents"] or 0)),
            "client_href": f"/admin/clients/{row['id']}",
        }
        for row in rows
    ]


def recent_listings_for_brokerage(brokerage_id: int, *, limit: int = 3) -> list[dict]:
    rows = db.all_(
        """SELECT l.id, l.title, l.status, l.created_at, l.shoot_date,
                  a.id AS agent_id, a.name AS agent_name
             FROM listings l
             JOIN clients a
               ON a.id=l.client_id
              AND a.studio_id=l.studio_id
              AND a.client_type='agent'
            WHERE l.studio_id=? AND a.parent_id=?
            ORDER BY COALESCE(l.shoot_date, l.created_at) DESC, l.id DESC
            LIMIT ?""",
        (STUDIO_ID, brokerage_id, limit),
    )
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "created_at": row["created_at"],
            "shoot_date": row["shoot_date"],
            "agent_id": row["agent_id"],
            "agent_name": row["agent_name"],
            "listing_href": f"/admin/listings/{row['id']}",
            "agent_href": f"/admin/clients/{row['agent_id']}",
        }
        for row in rows
    ]


def _hydrate_account(row: Any) -> dict:
    paid_cents = int(row["paid_cents"] or 0)
    open_cents = int(row["open_cents"] or 0)
    account = {
        "id": row["id"],
        "name": row["name"],
        "company": row["company"],
        "email": row["email"],
        "n_agents": int(row["n_agents"] or 0),
        "n_listings": int(row["n_listings"] or 0),
        "n_paid_invoices": int(row["n_paid_invoices"] or 0),
        "n_open_invoices": int(row["n_open_invoices"] or 0),
        "paid_cents": paid_cents,
        "open_cents": open_cents,
        "portfolio_cents": paid_cents + open_cents,
        "paid_display": _money(paid_cents),
        "open_display": _money(open_cents),
        "portfolio_display": _money(paid_cents + open_cents),
        "last_listing_at": row["last_listing_at"],
        "client_href": f"/admin/clients/{row['id']}",
        "statement_href": f"/admin/reports/brokerage/{row['id']}",
    }
    account["top_agents"] = top_agents_for_brokerage(row["id"])
    account["recent_listings"] = recent_listings_for_brokerage(row["id"])
    return account


def brokerage_accounts(*, limit: int = 50) -> list[dict]:
    """Brokerage portfolio rows with agent, listing, paid, and open value signals."""

    match_brokerage = _invoice_matches_brokerage_clause()
    rows = db.all_(
        f"""SELECT * FROM (
             SELECT c.id, c.name, c.company, c.email,
                    (SELECT COUNT(*)
                       FROM clients a
                      WHERE a.studio_id=c.studio_id
                        AND a.parent_id=c.id
                        AND a.client_type='agent') AS n_agents,
                    (SELECT COUNT(*)
                       FROM listings l
                       JOIN clients a
                         ON a.id=l.client_id
                        AND a.studio_id=l.studio_id
                        AND a.client_type='agent'
                      WHERE l.studio_id=c.studio_id
                        AND a.parent_id=c.id) AS n_listings,
                    (SELECT MAX(l.created_at)
                       FROM listings l
                       JOIN clients a
                         ON a.id=l.client_id
                        AND a.studio_id=l.studio_id
                        AND a.client_type='agent'
                      WHERE l.studio_id=c.studio_id
                        AND a.parent_id=c.id) AS last_listing_at,
                    (SELECT COUNT(*)
                       FROM invoices i
                       LEFT JOIN listings li
                         ON li.id=i.listing_id AND li.studio_id=i.studio_id
                      WHERE i.studio_id=c.studio_id
                        AND i.status='paid'
                        AND {match_brokerage}) AS n_paid_invoices,
                    (SELECT COUNT(*)
                       FROM invoices i
                       LEFT JOIN listings li
                         ON li.id=i.listing_id AND li.studio_id=i.studio_id
                      WHERE i.studio_id=c.studio_id
                        AND i.status='sent'
                        AND {match_brokerage}) AS n_open_invoices,
                    COALESCE((SELECT SUM(i.amount_cents)
                       FROM invoices i
                       LEFT JOIN listings li
                         ON li.id=i.listing_id AND li.studio_id=i.studio_id
                      WHERE i.studio_id=c.studio_id
                        AND i.status='paid'
                        AND {match_brokerage}), 0) AS paid_cents,
                    COALESCE((SELECT SUM(i.amount_cents)
                       FROM invoices i
                       LEFT JOIN listings li
                         ON li.id=i.listing_id AND li.studio_id=i.studio_id
                      WHERE i.studio_id=c.studio_id
                        AND i.status='sent'
                        AND {match_brokerage}), 0) AS open_cents
               FROM clients c
              WHERE c.studio_id=? AND c.client_type='brokerage'
           ) accounts
           WHERE n_agents > 0 OR n_listings > 0 OR paid_cents > 0 OR open_cents > 0
           ORDER BY paid_cents DESC, open_cents DESC, n_listings DESC, last_listing_at DESC
           LIMIT ?""",
        (STUDIO_ID, limit),
    )
    return [_hydrate_account(row) for row in rows]


def brokerage_summary(rows: list[dict] | None = None) -> dict:
    rows = rows if rows is not None else brokerage_accounts(limit=100)
    paid_cents = sum(row["paid_cents"] for row in rows)
    open_cents = sum(row["open_cents"] for row in rows)
    n_agents = sum(row["n_agents"] for row in rows)
    n_listings = sum(row["n_listings"] for row in rows)
    return {
        "n_brokerages": len(rows),
        "n_agents": n_agents,
        "n_listings": n_listings,
        "paid_cents": paid_cents,
        "open_cents": open_cents,
        "portfolio_cents": paid_cents + open_cents,
        "paid_display": _money(paid_cents),
        "open_display": _money(open_cents),
        "portfolio_display": _money(paid_cents + open_cents),
    }


def brokerage_accounts_csv() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "brokerage",
            "company",
            "agents",
            "listings",
            "paid_cents",
            "open_cents",
            "paid_invoices",
            "open_invoices",
            "last_listing_at",
            "top_agents",
            "recent_listings",
        ]
    )
    for row in brokerage_accounts(limit=100):
        writer.writerow(
            [
                row["name"],
                row["company"] or "",
                row["n_agents"],
                row["n_listings"],
                row["paid_cents"],
                row["open_cents"],
                row["n_paid_invoices"],
                row["n_open_invoices"],
                (row["last_listing_at"] or "")[:10],
                "; ".join(a["name"] for a in row["top_agents"]),
                "; ".join(listing["title"] for listing in row["recent_listings"]),
            ]
        )
    return buf.getvalue()
