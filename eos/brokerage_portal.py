"""Brokerage self-serve portal — account summary, invoices, and agent deliveries."""

from fastapi import HTTPException

from . import clients, db
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


def _money(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${dollars:,.0f}"
    return f"${dollars:,.2f}"


def _invoice_rows(brokerage_id: int, *, limit: int = 50) -> list[dict]:
    rows = db.all_(
        """SELECT i.id, i.slug, i.title, i.amount_cents, i.status, i.created_at, i.paid_at,
                  l.title AS listing_title,
                  agent.name AS agent_name
           FROM invoices i
           LEFT JOIN listings l
             ON l.id=i.listing_id AND l.studio_id=i.studio_id
           LEFT JOIN clients agent
             ON agent.id=COALESCE(i.agent_client_id, i.client_id, l.client_id)
            AND agent.studio_id=i.studio_id
           WHERE i.studio_id=?
             AND i.status IN ('sent','paid')
             AND (i.bill_to_client_id=? OR agent.parent_id=?)
           ORDER BY CASE WHEN i.status='sent' THEN 0 ELSE 1 END,
                    COALESCE(i.paid_at, i.created_at) DESC,
                    i.id DESC
           LIMIT ?""",
        (STUDIO_ID, brokerage_id, brokerage_id, limit),
    )
    return [
        {
            "id": row["id"],
            "slug": row["slug"],
            "title": row["title"],
            "amount_cents": int(row["amount_cents"] or 0),
            "amount_display": _money(int(row["amount_cents"] or 0)),
            "status": row["status"],
            "created_at": row["created_at"],
            "paid_at": row["paid_at"],
            "listing_title": row["listing_title"],
            "agent_name": row["agent_name"],
            "invoice_href": f"/i/{row['slug']}",
        }
        for row in rows
    ]


def account_summary(brokerage_id: int) -> dict:
    clients.get_client(brokerage_id)
    row = db.one(
        """SELECT
                  (SELECT COUNT(*)
                     FROM clients a
                    WHERE a.studio_id=? AND a.parent_id=? AND a.client_type='agent') AS n_agents,
                  (SELECT COUNT(*)
                     FROM listings l
                     JOIN clients a
                       ON a.id=l.client_id
                      AND a.studio_id=l.studio_id
                      AND a.client_type='agent'
                    WHERE l.studio_id=? AND a.parent_id=?) AS n_listings,
                  (SELECT COUNT(*)
                     FROM listings l
                     JOIN clients a
                       ON a.id=l.client_id
                      AND a.studio_id=l.studio_id
                      AND a.client_type='agent'
                    WHERE l.studio_id=? AND a.parent_id=? AND l.status='delivered') AS n_delivered,
                  (SELECT MAX(COALESCE(l.shoot_date, l.created_at))
                     FROM listings l
                     JOIN clients a
                       ON a.id=l.client_id
                      AND a.studio_id=l.studio_id
                      AND a.client_type='agent'
                    WHERE l.studio_id=? AND a.parent_id=?) AS last_activity_at""",
        (
            STUDIO_ID,
            brokerage_id,
            STUDIO_ID,
            brokerage_id,
            STUDIO_ID,
            brokerage_id,
            STUDIO_ID,
            brokerage_id,
        ),
    )
    invoice_totals = db.all_(
        """SELECT i.status, COUNT(*) AS n, COALESCE(SUM(i.amount_cents), 0) AS cents
           FROM invoices i
           LEFT JOIN listings l
             ON l.id=i.listing_id AND l.studio_id=i.studio_id
           LEFT JOIN clients agent
             ON agent.id=COALESCE(i.agent_client_id, i.client_id, l.client_id)
            AND agent.studio_id=i.studio_id
           WHERE i.studio_id=?
             AND i.status IN ('sent','paid')
             AND (i.bill_to_client_id=? OR agent.parent_id=?)
           GROUP BY i.status""",
        (STUDIO_ID, brokerage_id, brokerage_id),
    )
    by_status = {r["status"]: r for r in invoice_totals}
    sent = by_status.get("sent")
    paid = by_status.get("paid")
    open_cents = int(sent["cents"] if sent else 0)
    paid_cents = int(paid["cents"] if paid else 0)
    return {
        "n_agents": int(row["n_agents"] or 0) if row else 0,
        "n_listings": int(row["n_listings"] or 0) if row else 0,
        "n_delivered": int(row["n_delivered"] or 0) if row else 0,
        "last_activity_at": row["last_activity_at"] if row else None,
        "n_open": int(sent["n"] if sent else 0),
        "n_paid": int(paid["n"] if paid else 0),
        "open_cents": open_cents,
        "paid_cents": paid_cents,
        "portfolio_cents": open_cents + paid_cents,
        "open_display": _money(open_cents),
        "paid_display": _money(paid_cents),
        "portfolio_display": _money(open_cents + paid_cents),
    }


def agent_activity(brokerage_id: int, *, limit: int = 20) -> list[dict]:
    rows = db.all_(
        """SELECT a.id, a.name, a.company,
                  (SELECT COUNT(*)
                     FROM listings l
                    WHERE l.studio_id=a.studio_id AND l.client_id=a.id) AS n_listings,
                  (SELECT COUNT(*)
                     FROM listings l
                    WHERE l.studio_id=a.studio_id AND l.client_id=a.id AND l.status='delivered') AS n_delivered,
                  (SELECT MAX(COALESCE(l.shoot_date, l.created_at))
                     FROM listings l
                    WHERE l.studio_id=a.studio_id AND l.client_id=a.id) AS last_activity_at,
                  COALESCE((SELECT SUM(i.amount_cents)
                     FROM invoices i
                     LEFT JOIN listings li
                       ON li.id=i.listing_id AND li.studio_id=i.studio_id
                    WHERE i.studio_id=a.studio_id
                      AND i.status='paid'
                      AND (
                           i.agent_client_id=a.id
                           OR (i.agent_client_id IS NULL AND i.client_id=a.id)
                           OR (i.agent_client_id IS NULL AND i.client_id IS NULL AND li.client_id=a.id)
                      )), 0) AS paid_cents,
                  COALESCE((SELECT SUM(i.amount_cents)
                     FROM invoices i
                     LEFT JOIN listings li
                       ON li.id=i.listing_id AND li.studio_id=i.studio_id
                    WHERE i.studio_id=a.studio_id
                      AND i.status='sent'
                      AND (
                           i.agent_client_id=a.id
                           OR (i.agent_client_id IS NULL AND i.client_id=a.id)
                           OR (i.agent_client_id IS NULL AND i.client_id IS NULL AND li.client_id=a.id)
                      )), 0) AS open_cents
           FROM clients a
           WHERE a.studio_id=? AND a.parent_id=? AND a.client_type='agent'
           ORDER BY paid_cents DESC, open_cents DESC, n_listings DESC, last_activity_at DESC
           LIMIT ?""",
        (STUDIO_ID, brokerage_id, limit),
    )
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "company": row["company"],
            "n_listings": int(row["n_listings"] or 0),
            "n_delivered": int(row["n_delivered"] or 0),
            "last_activity_at": row["last_activity_at"],
            "paid_cents": int(row["paid_cents"] or 0),
            "open_cents": int(row["open_cents"] or 0),
            "paid_display": _money(int(row["paid_cents"] or 0)),
            "open_display": _money(int(row["open_cents"] or 0)),
        }
        for row in rows
    ]


def agent_deliveries(brokerage_id: int) -> list[dict]:
    rows = db.all_(
        """SELECT l.id, l.title, l.status, l.address_line1, l.created_at, l.shoot_date,
                  l.site_slug, l.site_published,
                  c.name AS agent_name,
                  (SELECT g.slug
                     FROM galleries g
                    WHERE g.listing_id=l.id AND g.studio_id=l.studio_id AND g.published=1
                    ORDER BY g.id DESC LIMIT 1) AS gallery_slug,
                  (SELECT g.pin
                     FROM galleries g
                    WHERE g.listing_id=l.id AND g.studio_id=l.studio_id AND g.published=1
                    ORDER BY g.id DESC LIMIT 1) AS gallery_pin,
                  (SELECT i.slug
                     FROM invoices i
                    WHERE i.listing_id=l.id AND i.studio_id=l.studio_id AND i.status='sent'
                    ORDER BY i.id DESC LIMIT 1) AS unpaid_invoice_slug,
                  (SELECT i.status
                     FROM invoices i
                    WHERE i.listing_id=l.id AND i.studio_id=l.studio_id AND i.status='paid'
                    ORDER BY i.paid_at DESC LIMIT 1) AS has_paid
           FROM listings l
           JOIN clients c
             ON c.id=l.client_id
            AND c.studio_id=l.studio_id
            AND c.client_type='agent'
           WHERE l.studio_id=? AND c.parent_id=?
           ORDER BY COALESCE(l.shoot_date, l.created_at) DESC, l.id DESC
           LIMIT 100""",
        (STUDIO_ID, brokerage_id),
    )
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "address_line1": row["address_line1"],
            "created_at": row["created_at"],
            "shoot_date": row["shoot_date"],
            "site_slug": row["site_slug"],
            "site_published": row["site_published"],
            "agent_name": row["agent_name"],
            "gallery_slug": row["gallery_slug"],
            "gallery_pin": row["gallery_pin"],
            "gallery_published": bool(row["gallery_slug"]),
            "unpaid_invoice_slug": row["unpaid_invoice_slug"],
            "has_paid": row["has_paid"],
        }
        for row in rows
    ]


def portal_summary(brokerage_id: int) -> dict:
    invoices = _invoice_rows(brokerage_id, limit=100)
    return {
        "totals": account_summary(brokerage_id),
        "open_invoices": [r for r in invoices if r["status"] == "sent"],
        "paid_invoices": [r for r in invoices if r["status"] == "paid"],
        "agent_activity": agent_activity(brokerage_id),
        "deliveries": agent_deliveries(brokerage_id),
    }
