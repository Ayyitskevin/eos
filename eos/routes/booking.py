"""Public booking confirmation page."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import commerce, db, stripe_checkout
from ..render import templates
from ..vocab import STUDIO_ID

router = APIRouter()


@router.get("/booking/{token}", response_class=HTMLResponse)
async def booking_confirm(request: Request, token: str):
    order = commerce.get_order_by_token(token)
    pkg = (
        db.one(
            "SELECT name FROM service_packages WHERE id=? AND studio_id=?",
            (order["package_id"], STUDIO_ID),
        )
        if order["package_id"]
        else None
    )
    listing = (
        db.one(
            "SELECT title, address_line1 FROM listings WHERE id=? AND studio_id=?",
            (order["listing_id"], STUDIO_ID),
        )
        if order["listing_id"]
        else None
    )
    inv = (
        db.one(
            "SELECT slug, status FROM invoices WHERE id=? AND studio_id=?",
            (order["invoice_id"], STUDIO_ID),
        )
        if order["invoice_id"]
        else None
    )
    return templates.TemplateResponse(
        request,
        "public/booking_confirm.html",
        {
            "order": order,
            "package_name": pkg["name"] if pkg else "",
            "listing": listing,
            "invoice": inv,
            "payments_on": stripe_checkout.payments_configured(),
            "thanks": request.query_params.get("thanks"),
        },
    )
