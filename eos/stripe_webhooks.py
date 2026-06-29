"""Shared Stripe webhook handlers — client payments + platform billing."""

from __future__ import annotations

import logging

from . import automations, db, invoices, tenant, upsell

log = logging.getLogger("eos.stripe_webhooks")


def handle_invoice_checkout_completed(session: dict) -> bool:
    """Mark invoice paid after Connect or legacy Checkout. Returns True if handled."""
    if session.get("mode") != "payment":
        return False
    raw_id = session.get("metadata", {}).get("invoice_id")
    if not raw_id:
        return False
    try:
        invoice_id = int(raw_id)
    except (TypeError, ValueError):
        return False
    inv = db.one(
        "SELECT listing_id, invoice_kind, inquiry_id, studio_id, status FROM invoices WHERE id=?",
        (invoice_id,),
    )
    if not inv or inv["status"] == "paid":
        return False
    previous_studio = tenant.get_studio_id()
    tenant.set_studio(inv["studio_id"])
    try:
        invoices.mark_paid(invoice_id)
        upsell.mark_paid(invoice_id)
        if inv["invoice_kind"] == "deposit" and inv["inquiry_id"]:
            automations.on_deposit_paid(inv["inquiry_id"], inv["listing_id"])
        elif inv["listing_id"]:
            automations.on_invoice_paid(inv["listing_id"])
        log.info("invoice %s paid via stripe checkout %s", invoice_id, session.get("id"))
        return True
    finally:
        tenant.set_studio(previous_studio)
