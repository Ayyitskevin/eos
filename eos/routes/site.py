import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import commerce, config, security, scheduling, studio
from ..render import templates

router = APIRouter()
INDEXABLE = {"/", "/book"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _book_context(error: str | None = None, thanks: bool = False):
    profile = studio.get_profile()
    return {
        "profile": profile,
        "packages": studio.list_packages(active_only=True),
        "addons": studio.list_addons(active_only=True),
        "slots": scheduling.open_slots(),
        "terms": commerce.BOOKING_TERMS.format(site_name=config.SITE_NAME),
        "payments_on": bool(config.STRIPE_SECRET_KEY),
        "error": error,
        "thanks": thanks,
    }


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    profile = studio.get_profile()
    packages = studio.list_packages(active_only=True)
    return templates.TemplateResponse(
        request, "site/home.html",
        {"profile": profile, "packages": packages},
    )


@router.get("/book", response_class=HTMLResponse)
async def book_form(request: Request):
    return templates.TemplateResponse(request, "site/book.html", _book_context())


@router.post("/book")
async def book_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    property_address: str = Form(...),
    package_id: int = Form(...),
    scheduled_at: str = Form(...),
    signer_name: str = Form(...),
    sqft: int = Form(0),
    message: str = Form(""),
    promo_code: str = Form(""),
    addon_ids: list[int] = Form(default=[]),
):
    ip = security.client_ip(request)
    if security.inquiry_throttled(ip, security.INQUIRY_BUCKET_BOOK):
        raise HTTPException(status_code=429, detail="too many requests")
    email = email.strip().lower()
    if not _EMAIL.match(email):
        return templates.TemplateResponse(
            request, "site/book.html",
            _book_context(error="Invalid email."),
            status_code=400,
        )
    if not property_address.strip():
        return templates.TemplateResponse(
            request, "site/book.html",
            _book_context(error="Property address is required."),
            status_code=400,
        )

    security.inquiry_record(ip, security.INQUIRY_BUCKET_BOOK)
    try:
        result = commerce.create_booking(
            name=name,
            email=email,
            phone=phone,
            property_address=property_address,
            package_id=package_id,
            scheduled_at=scheduled_at,
            addon_ids=addon_ids,
            sqft=sqft or None,
            message=message,
            signer_name=signer_name,
            promo_code=promo_code,
        )
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else "Booking failed."
        return templates.TemplateResponse(
            request, "site/book.html",
            _book_context(error=detail),
            status_code=e.status_code,
        )

    if result["pay_slug"] and config.STRIPE_SECRET_KEY:
        return RedirectResponse(f"/i/{result['pay_slug']}", status_code=303)
    return RedirectResponse(f"/booking/{result['order_token']}", status_code=303)