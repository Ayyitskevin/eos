"""Listing-scoped add-on checkout from delivery surfaces."""

import json
import logging

from fastapi import HTTPException

from . import clients, db, invoices, security, stripe_checkout, tenant
from .vocab import STUDIO_ID

log = logging.getLogger("eos.upsell")


def list_delivery_addons() -> list:
    return db.all_(
        """SELECT * FROM service_addons
           WHERE studio_id=? AND active=1
           ORDER BY position""",
        (STUDIO_ID,),
    )


def _addon_rows(addon_ids: list[int]) -> list:
    if not addon_ids:
        return []
    ph = ",".join("?" * len(addon_ids))
    return db.all_(
        f"SELECT * FROM service_addons WHERE id IN ({ph}) AND studio_id=? AND active=1",
        (*addon_ids, STUDIO_ID),
    )


def _order_by_token(order_token: str):
    row = db.one(
        "SELECT * FROM listing_upsell_orders WHERE token=? AND studio_id=?",
        (order_token, STUDIO_ID),
    )
    if not row or row["status"] != "pending":
        raise HTTPException(status_code=404)
    return row


def create_order(
    *,
    listing_id: int,
    addon_ids: list[int],
    client_id: int | None = None,
) -> dict:
    from . import listings

    listing = listings.get_listing(listing_id)
    order_client_id = client_id or listing["client_id"]
    if order_client_id is not None:
        clients.get_client(order_client_id)
    addons = _addon_rows(addon_ids)
    if not addons:
        raise HTTPException(status_code=400, detail="Select at least one add-on.")
    items = [{"label": a["name"], "qty": 1, "unit_cents": a["price_cents"]} for a in addons]
    total = sum(a["price_cents"] for a in addons)
    token = security.new_token()
    oid = db.run(
        """INSERT INTO listing_upsell_orders
           (studio_id, listing_id, client_id, addon_ids, amount_cents, token)
           VALUES (?,?,?,?,?,?)""",
        (
            STUDIO_ID,
            listing_id,
            order_client_id,
            json.dumps(addon_ids),
            total,
            token,
        ),
    )
    title = f"Add-ons — {listing['title']}"
    iid = invoices.create_invoice(
        listing_id,
        title=title,
        amount_cents=total,
        client_id=order_client_id,
        line_items=items,
        notes="Delivery upsell",
        invoice_kind="balance",
    )
    inv = invoices.get_invoice(iid)
    db.run(
        "UPDATE listing_upsell_orders SET invoice_id=? WHERE id=? AND studio_id=?",
        (iid, oid, STUDIO_ID),
    )
    db.audit("upsell", "order.create", f"listing={listing_id} total={total}")
    return {"order_id": oid, "token": token, "invoice": inv, "total_cents": total}


def checkout_url(order_token: str) -> str:
    row = _order_by_token(order_token)
    if not row["invoice_id"]:
        raise HTTPException(status_code=400, detail="invoice missing")
    inv = invoices.get_invoice(row["invoice_id"])
    base = tenant.get_base_url()
    if inv["status"] == "paid":
        return f"{base}/i/{inv['slug']}?thanks=1"
    if not stripe_checkout.payments_configured():
        return f"{base}/i/{inv['slug']}"
    client = None
    if inv["client_id"]:
        client = db.one(
            "SELECT email FROM clients WHERE id=? AND studio_id=?",
            (inv["client_id"], STUDIO_ID),
        )
    session = stripe_checkout.create_payment_session(
        amount_cents=inv["amount_cents"],
        title=inv["title"],
        customer_email=client["email"] if client and client["email"] else None,
        metadata={"invoice_id": str(inv["id"]), "upsell_order_id": str(row["id"])},
        success_url=f"{base}/i/{inv['slug']}?thanks=1",
        cancel_url=f"{base}/upsell/{order_token}",
        existing_session_id=inv.get("stripe_session_id"),
    )
    db.run(
        "UPDATE invoices SET stripe_session_id=? WHERE id=? AND studio_id=?",
        (session.id, inv["id"], STUDIO_ID),
    )
    db.run(
        "UPDATE listing_upsell_orders SET stripe_session_id=? WHERE id=? AND studio_id=?",
        (session.id, row["id"], STUDIO_ID),
    )
    return session.url


def mark_paid(invoice_id: int) -> None:
    row = db.one(
        "SELECT id FROM listing_upsell_orders WHERE invoice_id=? AND studio_id=?",
        (invoice_id, STUDIO_ID),
    )
    if row:
        db.run(
            "UPDATE listing_upsell_orders SET status='paid' WHERE id=? AND studio_id=?",
            (row["id"], STUDIO_ID),
        )
