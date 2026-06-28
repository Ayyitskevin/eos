"""Listing-scoped add-on checkout from delivery surfaces."""

import json
import logging

import stripe
from fastapi import HTTPException

from . import config, db, invoices, security
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


def create_order(
    *,
    listing_id: int,
    addon_ids: list[int],
    client_id: int | None = None,
) -> dict:
    from . import listings

    listing = listings.get_listing(listing_id)
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
            client_id or listing["client_id"],
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
        client_id=client_id or listing["client_id"],
        line_items=items,
        notes="Delivery upsell",
        invoice_kind="balance",
    )
    inv = invoices.get_invoice(iid)
    db.run(
        "UPDATE listing_upsell_orders SET invoice_id=? WHERE id=?",
        (iid, oid),
    )
    db.audit("upsell", "order.create", f"listing={listing_id} total={total}")
    return {"order_id": oid, "token": token, "invoice": inv, "total_cents": total}


def checkout_url(order_token: str) -> str:
    row = db.one(
        "SELECT * FROM listing_upsell_orders WHERE token=? AND studio_id=?",
        (order_token, STUDIO_ID),
    )
    if not row or row["status"] != "pending":
        raise HTTPException(status_code=404)
    if not row["invoice_id"]:
        raise HTTPException(status_code=400, detail="invoice missing")
    inv = invoices.get_invoice(row["invoice_id"])
    if inv["status"] == "paid":
        return f"{config.BASE_URL}/i/{inv['slug']}?thanks=1"
    if not config.STRIPE_SECRET_KEY:
        return f"{config.BASE_URL}/i/{inv['slug']}"
    if inv.get("stripe_session_id"):
        try:
            session = stripe.checkout.Session.retrieve(
                inv["stripe_session_id"],
                api_key=config.STRIPE_SECRET_KEY,
            )
            if session.url:
                return session.url
        except Exception:
            pass
    client = None
    if inv["client_id"]:
        client = db.one("SELECT email FROM clients WHERE id=?", (inv["client_id"],))
    session = stripe.checkout.Session.create(
        api_key=config.STRIPE_SECRET_KEY,
        mode="payment",
        payment_method_types=["card"],
        line_items=[
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": inv["amount_cents"],
                    "product_data": {"name": inv["title"]},
                },
            }
        ],
        customer_email=client["email"] if client and client["email"] else None,
        metadata={"invoice_id": str(inv["id"]), "upsell_order_id": str(row["id"])},
        success_url=f"{config.BASE_URL}/i/{inv['slug']}?thanks=1",
        cancel_url=f"{config.BASE_URL}/upsell/{order_token}",
    )
    db.run("UPDATE invoices SET stripe_session_id=? WHERE id=?", (session.id, inv["id"]))
    db.run(
        "UPDATE listing_upsell_orders SET stripe_session_id=? WHERE id=?",
        (session.id, row["id"]),
    )
    return session.url


def mark_paid(invoice_id: int) -> None:
    row = db.one(
        "SELECT id FROM listing_upsell_orders WHERE invoice_id=? AND studio_id=?",
        (invoice_id, STUDIO_ID),
    )
    if row:
        db.run(
            "UPDATE listing_upsell_orders SET status='paid' WHERE id=?",
            (row["id"],),
        )
