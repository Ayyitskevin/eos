import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import commerce, config, scheduling, security, stripe_checkout, studio, tenant
from ..render import templates

router = APIRouter()
INDEXABLE = {"/", "/book", "/book/homeowner", "/signup", "/demo"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _book_context(error: str | None = None, thanks: bool = False):
    profile = studio.get_profile()
    addons = studio.list_addons(active_only=True)
    twilight_addon = next((a for a in addons if a["slug"] == "twilight"), None)
    day_slots = scheduling.open_slots()
    twilight_only = scheduling.twilight_slots()
    slots = day_slots + twilight_only
    return {
        "profile": profile,
        "packages": studio.list_packages(active_only=True),
        "addons": addons,
        "twilight_addon": twilight_addon,
        "slots": slots,
        "day_slots": day_slots,
        "twilight_slots": twilight_only,
        "terms": commerce.BOOKING_TERMS.format(site_name=config.SITE_NAME),
        "payments_on": stripe_checkout.payments_configured(),
        "error": error,
        "thanks": thanks,
    }


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if config.SAAS_MODE and tenant.get_studio_id() == "default":
        return templates.TemplateResponse(
            request,
            "site/marketing.html",
            {
                "signup_enabled": config.SIGNUP_ENABLED,
                "base_domain": config.BASE_DOMAIN,
            },
        )
    profile = studio.get_profile()
    packages = studio.list_packages(active_only=True)
    return templates.TemplateResponse(
        request,
        "site/home.html",
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
            request,
            "site/book.html",
            _book_context(error="Invalid email."),
            status_code=400,
        )
    if not property_address.strip():
        return templates.TemplateResponse(
            request,
            "site/book.html",
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
            request,
            "site/book.html",
            _book_context(error=detail),
            status_code=e.status_code,
        )

    if result["pay_slug"] and stripe_checkout.payments_configured():
        return RedirectResponse(f"/i/{result['pay_slug']}", status_code=303)
    return RedirectResponse(f"/booking/{result['order_token']}", status_code=303)


@router.get("/book/homeowner", response_class=HTMLResponse)
async def book_homeowner_form(request: Request):
    ctx = _book_context()
    ctx["terms"] = commerce.BOOKING_TERMS.format(site_name=config.SITE_NAME)
    return templates.TemplateResponse(request, "site/book_homeowner.html", ctx)


@router.post("/book/homeowner")
async def book_homeowner_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    property_address: str = Form(...),
    package_id: int = Form(...),
    scheduled_at: str = Form(...),
    signer_name: str = Form(...),
    sqft: int = Form(0),
    message: str = Form(""),
):
    ip = security.client_ip(request)
    if security.inquiry_throttled(ip, security.INQUIRY_BUCKET_BOOK):
        raise HTTPException(status_code=429, detail="too many requests")
    email = email.strip().lower()
    if not _EMAIL.match(email):
        return templates.TemplateResponse(
            request,
            "site/book_homeowner.html",
            _book_context(error="Invalid email."),
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
            sqft=sqft or None,
            message=message,
            signer_name=signer_name,
            client_type="homeowner",
        )
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else "Booking failed."
        return templates.TemplateResponse(
            request,
            "site/book_homeowner.html",
            _book_context(error=detail),
            status_code=e.status_code,
        )
    if result["pay_slug"] and stripe_checkout.payments_configured():
        return RedirectResponse(f"/i/{result['pay_slug']}", status_code=303)
    return RedirectResponse(f"/booking/{result['order_token']}", status_code=303)
