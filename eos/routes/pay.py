"""Public invoice view + Stripe Checkout."""

import json
import logging

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import automations, config, db, invoices, security
from ..render import templates

log = logging.getLogger("eos.routes.pay")
router = APIRouter()


@router.get("/i/{slug}", response_class=HTMLResponse)
async def view_invoice(request: Request, slug: str):
    inv = invoices.get_invoice_by_slug(slug)
    client = None
    if inv["client_id"]:
        client = db.one("SELECT name, email, company FROM clients WHERE id=?", (inv["client_id"],))
    listing = None
    if inv["listing_id"]:
        listing = db.one("SELECT title, address_line1, city FROM listings WHERE id=?", (inv["listing_id"],))
    return templates.TemplateResponse(
        request, "public/invoice.html",
        {
            "inv": inv,
            "client": client,
            "listing": listing,
            "items": json.loads(inv["line_items"] or "[]"),
            "payments_on": bool(config.STRIPE_SECRET_KEY),
            "thanks": request.query_params.get("thanks"),
        },
    )


@router.post("/i/{slug}/pay")
async def pay_invoice(slug: str):
    inv = invoices.get_invoice_by_slug(slug)
    if inv["status"] == "paid":
        raise HTTPException(status_code=400, detail="already paid")
    if not config.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="online payment is not configured")
    client = db.one("SELECT email FROM clients WHERE id=?", (inv["client_id"],)) if inv["client_id"] else None
    success = f"{config.BASE_URL}/i/{slug}?thanks=1"
    if inv.get("invoice_kind") == "deposit" and inv.get("inquiry_id"):
        inq = db.one("SELECT order_token FROM inquiries WHERE id=?", (inv["inquiry_id"],))
        if inq and inq.get("order_token"):
            success = f"{config.BASE_URL}/booking/{inq['order_token']}?thanks=1"
    session = stripe.checkout.Session.create(
        api_key=config.STRIPE_SECRET_KEY,
        mode="payment",
        payment_method_types=["card"],
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": inv["amount_cents"],
                "product_data": {"name": inv["title"]},
            },
        }],
        customer_email=client["email"] if client and client["email"] else None,
        metadata={"invoice_id": str(inv["id"])},
        success_url=success,
        cancel_url=f"{config.BASE_URL}/i/{slug}",
    )
    db.run("UPDATE invoices SET stripe_session_id=? WHERE id=?", (session.id, inv["id"]))
    log.info("invoice %s checkout %s", inv["id"], session.id)
    return RedirectResponse(session.url, status_code=303)


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not config.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503)
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, config.STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400)
    if event["type"] == "checkout.session.completed":
        sess = event["data"]["object"]
        iid = sess.get("metadata", {}).get("invoice_id")
        if iid:
            inv = db.one(
                "SELECT listing_id, invoice_kind, inquiry_id FROM invoices WHERE id=?",
                (int(iid),),
            )
            invoices.mark_paid(int(iid))
            from .. import upsell as upsell_mod
            upsell_mod.mark_paid(int(iid))
            if inv:
                if inv.get("invoice_kind") == "deposit" and inv.get("inquiry_id"):
                    automations.on_deposit_paid(inv["inquiry_id"], inv["listing_id"])
                else:
                    automations.on_invoice_paid(inv["listing_id"])
            log.info("invoice %s paid via stripe", iid)
    return {"ok": True}