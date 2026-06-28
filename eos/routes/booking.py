"""Public booking confirmation page."""


from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import commerce, config, db
from ..render import templates

router = APIRouter()


@router.get("/booking/{token}", response_class=HTMLResponse)
async def booking_confirm(request: Request, token: str):
    order = commerce.get_order_by_token(token)
    pkg = (
        db.one("SELECT name FROM service_packages WHERE id=?", (order["package_id"],))
        if order["package_id"]
        else None
    )
    listing = (
        db.one("SELECT title, address_line1 FROM listings WHERE id=?", (order["listing_id"],))
        if order["listing_id"]
        else None
    )
    inv = (
        db.one("SELECT slug, status FROM invoices WHERE id=?", (order["invoice_id"],))
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
            "payments_on": bool(config.STRIPE_SECRET_KEY),
            "thanks": request.query_params.get("thanks"),
        },
    )
