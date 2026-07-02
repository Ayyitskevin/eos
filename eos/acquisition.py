"""Agent acquisition and referral tracking reports."""

import csv
import io

from . import db
from .vocab import STUDIO_ID


def _money(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${dollars:,.0f}"
    return f"${dollars:,.2f}"


def _agent_value(client_id: int) -> dict:
    row = db.one(
        """SELECT c.id, c.name, c.company, c.email,
                  parent.name AS brokerage_name,
                  (SELECT COUNT(*)
                     FROM listings l
                    WHERE l.studio_id=c.studio_id AND l.client_id=c.id) AS n_listings,
                  (SELECT MAX(l.created_at)
                     FROM listings l
                    WHERE l.studio_id=c.studio_id AND l.client_id=c.id) AS last_listing_at,
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
           WHERE c.studio_id=? AND c.id=? AND c.client_type='agent'""",
        (STUDIO_ID, client_id),
    )
    if not row:
        return {}
    paid_cents = int(row["paid_cents"] or 0)
    open_cents = int(row["open_cents"] or 0)
    n_listings = int(row["n_listings"] or 0)
    return {
        "id": row["id"],
        "name": row["name"],
        "company": row["company"],
        "email": row["email"],
        "brokerage_name": row["brokerage_name"],
        "n_listings": n_listings,
        "last_listing_at": row["last_listing_at"],
        "paid_cents": paid_cents,
        "open_cents": open_cents,
        "paid_display": _money(paid_cents),
        "open_display": _money(open_cents),
        "client_href": f"/admin/clients/{row['id']}",
    }


def referral_performance(limit: int = 50) -> list[dict]:
    rows = db.all_(
        """SELECT r.id, r.code, r.credit_cents, r.referrer_client_id, r.uses,
                  r.max_uses, r.active, r.created_at,
                  ref.name AS referrer_name,
                  ref.company AS referrer_company,
                  (SELECT COUNT(*)
                     FROM inquiries q
                    WHERE q.studio_id=r.studio_id
                      AND upper(q.promo_code)=upper(r.code)) AS n_inquiries,
                  (SELECT COUNT(DISTINCT q.listing_id)
                     FROM inquiries q
                    WHERE q.studio_id=r.studio_id
                      AND q.listing_id IS NOT NULL
                      AND upper(q.promo_code)=upper(r.code)) AS n_listings,
                  (SELECT MAX(q.created_at)
                     FROM inquiries q
                    WHERE q.studio_id=r.studio_id
                      AND upper(q.promo_code)=upper(r.code)) AS last_used_at,
                  COALESCE((SELECT SUM(i.amount_cents)
                     FROM invoices i
                    WHERE i.studio_id=r.studio_id
                      AND i.status='paid'
                      AND EXISTS (
                          SELECT 1
                            FROM inquiries q
                           WHERE q.studio_id=i.studio_id
                             AND q.listing_id=i.listing_id
                             AND upper(q.promo_code)=upper(r.code)
                      )), 0) AS referred_paid_cents,
                  COALESCE((SELECT SUM(i.amount_cents)
                     FROM invoices i
                    WHERE i.studio_id=r.studio_id
                      AND i.status='sent'
                      AND EXISTS (
                          SELECT 1
                            FROM inquiries q
                           WHERE q.studio_id=i.studio_id
                             AND q.listing_id=i.listing_id
                             AND upper(q.promo_code)=upper(r.code)
                      )), 0) AS referred_open_cents
           FROM referral_codes r
           LEFT JOIN clients ref
             ON ref.id=r.referrer_client_id
            AND ref.studio_id=r.studio_id
           WHERE r.studio_id=?
           ORDER BY referred_paid_cents DESC, r.uses DESC, r.created_at DESC
           LIMIT ?""",
        (STUDIO_ID, limit),
    )
    out = []
    for row in rows:
        paid_cents = int(row["referred_paid_cents"] or 0)
        open_cents = int(row["referred_open_cents"] or 0)
        referrer = _agent_value(row["referrer_client_id"]) if row["referrer_client_id"] else {}
        out.append(
            {
                "id": row["id"],
                "code": row["code"],
                "credit_cents": int(row["credit_cents"] or 0),
                "credit_display": _money(int(row["credit_cents"] or 0)),
                "referrer_client_id": row["referrer_client_id"],
                "referrer_name": row["referrer_name"],
                "referrer_company": row["referrer_company"],
                "referrer": referrer,
                "uses": int(row["uses"] or 0),
                "max_uses": row["max_uses"],
                "active": bool(row["active"]),
                "created_at": row["created_at"],
                "n_inquiries": int(row["n_inquiries"] or 0),
                "n_listings": int(row["n_listings"] or 0),
                "last_used_at": row["last_used_at"],
                "referred_paid_cents": paid_cents,
                "referred_open_cents": open_cents,
                "referred_paid_display": _money(paid_cents),
                "referred_open_display": _money(open_cents),
                "referrer_href": f"/admin/clients/{row['referrer_client_id']}"
                if row["referrer_client_id"]
                else "",
            }
        )
    return out


def intro_ask_queue(limit: int = 12) -> list[dict]:
    rows = db.all_(
        """SELECT * FROM (
             SELECT c.id, c.name, c.company, c.email,
                    parent.name AS brokerage_name,
                    (SELECT COUNT(*)
                       FROM listings l
                      WHERE l.studio_id=c.studio_id AND l.client_id=c.id) AS n_listings,
                    (SELECT MAX(l.created_at)
                       FROM listings l
                      WHERE l.studio_id=c.studio_id AND l.client_id=c.id) AS last_listing_at,
                    (SELECT COUNT(*)
                       FROM referral_codes r
                      WHERE r.studio_id=c.studio_id
                        AND r.referrer_client_id=c.id
                        AND r.active=1) AS n_active_codes,
                    (SELECT COALESCE(SUM(r.uses), 0)
                       FROM referral_codes r
                      WHERE r.studio_id=c.studio_id
                        AND r.referrer_client_id=c.id
                        AND r.active=1) AS referral_uses,
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
                        )), 0) AS paid_cents
               FROM clients c
               LEFT JOIN clients parent
                 ON parent.id=c.parent_id
                AND parent.studio_id=c.studio_id
                AND parent.client_type='brokerage'
              WHERE c.studio_id=? AND c.client_type='agent'
           ) agents
           WHERE (n_listings >= 2 OR paid_cents >= 40000)
             AND (n_active_codes = 0 OR referral_uses = 0)
           ORDER BY n_active_codes ASC, paid_cents DESC, n_listings DESC, last_listing_at DESC
           LIMIT ?""",
        (STUDIO_ID, limit),
    )
    out = []
    for row in rows:
        paid_cents = int(row["paid_cents"] or 0)
        n_active_codes = int(row["n_active_codes"] or 0)
        referral_uses = int(row["referral_uses"] or 0)
        if n_active_codes == 0:
            action = "Create referral code"
            reason = "High-value agent has no referral code."
        else:
            action = "Ask for an introduction"
            reason = "Referral code exists but has not converted yet."
        out.append(
            {
                "id": row["id"],
                "name": row["name"],
                "company": row["company"],
                "email": row["email"],
                "brokerage_name": row["brokerage_name"],
                "n_listings": int(row["n_listings"] or 0),
                "last_listing_at": row["last_listing_at"],
                "paid_cents": paid_cents,
                "paid_display": _money(paid_cents),
                "n_active_codes": n_active_codes,
                "referral_uses": referral_uses,
                "action": action,
                "reason": reason,
                "suggested_code": _suggested_code(row["name"]),
                "client_href": f"/admin/clients/{row['id']}",
            }
        )
    return out


def _suggested_code(name: str) -> str:
    letters = "".join(ch for ch in name.upper() if ch.isalnum())
    return f"{letters[:8] or 'AGENT'}25"


def summary(referrals: list[dict] | None = None, asks: list[dict] | None = None) -> dict:
    referrals = referrals if referrals is not None else referral_performance()
    asks = asks if asks is not None else intro_ask_queue()
    paid_cents = sum(row["referred_paid_cents"] for row in referrals)
    open_cents = sum(row["referred_open_cents"] for row in referrals)
    return {
        "n_codes": len(referrals),
        "n_active_codes": sum(1 for row in referrals if row["active"]),
        "n_referred_inquiries": sum(row["n_inquiries"] for row in referrals),
        "n_referred_listings": sum(row["n_listings"] for row in referrals),
        "n_intro_asks": len(asks),
        "referred_paid_cents": paid_cents,
        "referred_open_cents": open_cents,
        "referred_paid_display": _money(paid_cents),
        "referred_open_display": _money(open_cents),
    }


def dashboard() -> dict:
    referrals = referral_performance()
    asks = intro_ask_queue()
    return {
        "summary": summary(referrals, asks),
        "referrals": referrals,
        "intro_asks": asks,
    }


def acquisition_csv() -> str:
    data = dashboard()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Referral codes"])
    writer.writerow(
        [
            "code",
            "referrer",
            "uses",
            "max_uses",
            "credit_cents",
            "referred_inquiries",
            "referred_listings",
            "referred_paid_cents",
            "referred_open_cents",
            "last_used_at",
        ]
    )
    for row in data["referrals"]:
        writer.writerow(
            [
                row["code"],
                row["referrer_name"] or "",
                row["uses"],
                row["max_uses"] or "",
                row["credit_cents"],
                row["n_inquiries"],
                row["n_listings"],
                row["referred_paid_cents"],
                row["referred_open_cents"],
                (row["last_used_at"] or "")[:10],
            ]
        )
    writer.writerow([])
    writer.writerow(["Intro ask queue"])
    writer.writerow(
        [
            "agent",
            "company",
            "brokerage",
            "listings",
            "paid_cents",
            "active_codes",
            "referral_uses",
            "suggested_code",
            "action",
        ]
    )
    for row in data["intro_asks"]:
        writer.writerow(
            [
                row["name"],
                row["company"] or "",
                row["brokerage_name"] or "",
                row["n_listings"],
                row["paid_cents"],
                row["n_active_codes"],
                row["referral_uses"],
                row["suggested_code"],
                row["action"],
            ]
        )
    return buf.getvalue()
