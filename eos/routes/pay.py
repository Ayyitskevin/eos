"""Public invoice view + Stripe Checkout."""

import json
import logging

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import commerce, config, db, invoices, stripe_checkout, stripe_webhooks
from ..render import templates
from ..vocab import STUDIO_ID

log = logging.getLogger("eos.routes.pay")
router = APIRouter()


@router.get("/i/{slug}", response_class=HTMLResponse)
async def view_invoice(request: Request, slug: str):
    commerce.expire_pending_bookings()
    inv = dict(invoices.get_invoice_by_slug(slug))
    client = None
    if inv["client_id"]:
        client = db.one(
            "SELECT name, email, company FROM clients WHERE id=? AND studio_id=?",
            (inv["client_id"], STUDIO_ID),
        )
    listing = None
    if inv["listing_id"]:
        listing = db.one(
            "SELECT title, address_line1, city FROM listings WHERE id=? AND studio_id=?",
            (inv["listing_id"], STUDIO_ID),
        )
    resp = templates.TemplateResponse(
        request,
        "public/invoice.html",
        {
            "inv": inv,
            "client": client,
            "listing": listing,
            "items": json.loads(inv["line_items"] or "[]"),
            "payments_on": stripe_checkout.payments_configured(),
            "thanks": request.query_params.get("thanks"),
        },
    )
    if request.query_params.get("embed") == "1":
        from .. import studio

        resp.headers["Content-Security-Policy"] = (
            f"frame-ancestors {studio.embed_frame_ancestors()}"
        )
    return resp


@router.post("/i/{slug}/pay")
async def pay_invoice(slug: str):
    commerce.expire_pending_bookings()
    inv = dict(invoices.get_invoice_by_slug(slug))
    if inv["status"] == "paid":
        raise HTTPException(status_code=400, detail="already paid")
    if inv["status"] != "sent":
        raise HTTPException(status_code=409, detail="invoice is not payable")
    from .. import tenant

    client = (
        db.one(
            "SELECT email FROM clients WHERE id=? AND studio_id=?", (inv["client_id"], STUDIO_ID)
        )
        if inv["client_id"]
        else None
    )
    base = tenant.get_base_url()
    success = f"{base}/i/{slug}?thanks=1"
    if inv.get("invoice_kind") == "deposit" and inv.get("inquiry_id"):
        inq = db.one(
            "SELECT order_token FROM inquiries WHERE id=? AND studio_id=?",
            (inv["inquiry_id"], STUDIO_ID),
        )
        if inq and inq["order_token"]:
            success = f"{base}/booking/{inq['order_token']}?thanks=1"
    currency = inv.get("currency") or "usd"
    session = stripe_checkout.create_payment_session(
        amount_cents=inv["amount_cents"],
        title=inv["title"],
        customer_email=client["email"] if client and client["email"] else None,
        metadata={
            "invoice_id": str(inv["id"]),
            "studio_id": str(inv["studio_id"]),
            "currency": currency,
        },
        success_url=success,
        cancel_url=f"{base}/i/{slug}",
        existing_session_id=inv.get("stripe_session_id"),
        currency=currency,
    )
    log.info("invoice %s checkout %s", inv["id"], session.id)
    return RedirectResponse(session.url, status_code=303)


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not config.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503)
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not sig:
        raise HTTPException(status_code=400)
    try:
        event = stripe.Webhook.construct_event(payload, sig, config.STRIPE_WEBHOOK_SECRET)
    except Exception as exc:
        raise HTTPException(status_code=400) from exc
    if event["type"] == "checkout.session.completed":
        stripe_webhooks.handle_invoice_checkout_event(event)
    return {"ok": True}
