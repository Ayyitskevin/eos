"""Listing invoices — create, send, Stripe checkout when configured."""

import json

from fastapi import HTTPException

from . import db, security
from .vocab import STUDIO_ID


def get_invoice(invoice_id: int):
    row = db.one("SELECT * FROM invoices WHERE id=? AND studio_id=?", (invoice_id, STUDIO_ID))
    if not row:
        raise HTTPException(status_code=404)
    return row


def get_invoice_by_slug(slug: str):
    row = db.one("SELECT * FROM invoices WHERE slug=? AND studio_id=?", (slug, STUDIO_ID))
    if not row or row["status"] == "draft":
        raise HTTPException(status_code=404)
    return row


def list_for_listing(listing_id: int):
    return db.all_(
        "SELECT * FROM invoices WHERE listing_id=? ORDER BY created_at DESC",
        (listing_id,),
    )


def create_invoice(
    listing_id: int,
    *,
    title: str,
    amount_cents: int,
    client_id: int | None = None,
    line_items: list | None = None,
    notes: str = "",
) -> int:
    iid = db.run(
        """INSERT INTO invoices
           (studio_id, listing_id, client_id, slug, title, amount_cents, line_items, notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            STUDIO_ID, listing_id, client_id, security.new_slug(), title.strip(),
            amount_cents, json.dumps(line_items or []), notes.strip(),
        ),
    )
    db.audit("admin", "invoice.create", f"id={iid} listing_id={listing_id}")
    return iid


def update_invoice(invoice_id: int, **fields) -> None:
    allowed = {"title", "amount_cents", "status", "notes", "client_id"}
    parts = []
    params: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        parts.append(f"{k}=?")
        params.append(v)
    if not parts:
        return
    params.append(invoice_id)
    db.run(f"UPDATE invoices SET {', '.join(parts)} WHERE id=?", tuple(params))
    db.audit("admin", "invoice.update", f"id={invoice_id}")


def mark_sent(invoice_id: int) -> None:
    db.run(
        "UPDATE invoices SET status='sent' WHERE id=? AND status='draft'",
        (invoice_id,),
    )


def mark_paid(invoice_id: int) -> None:
    db.run(
        "UPDATE invoices SET status='paid', paid_at=datetime('now') WHERE id=?",
        (invoice_id,),
    )