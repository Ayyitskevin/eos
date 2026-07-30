import hashlib
import re
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import commerce, config, db, scheduling, security, stripe_checkout, studio, tenant
from ..render import templates
from ..vocab import STUDIO_ID

router = APIRouter()
INDEXABLE = {"/", "/book", "/book/homeowner", "/signup", "/demo", "/pricing", "/terms", "/privacy"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_booking_code(value: str | None) -> str:
    raw = (value or "").strip().upper()
    return "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "_"})[:40]


def _money(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${dollars:,.0f}"
    return f"${dollars:,.2f}"


def _require_bookable() -> None:
    readiness = tenant.public_booking_readiness()
    if not readiness["ready"]:
        raise HTTPException(status_code=404, detail="Online booking is not active for this studio.")


def _require_storefront_visible() -> None:
    """Keep SaaS tenant storefronts private until activation is complete."""
    studio_id = tenant.get_studio_id()
    if studio_id == "default" or not (config.SAAS_MODE or config.BASE_DOMAIN):
        return
    row = db.one(
        """SELECT s.active, s.signup_verified, p.published
           FROM studio s
           LEFT JOIN studio_profiles p ON p.studio_id=s.id
           WHERE s.id=?""",
        (studio_id,),
    )
    if not row or not (row["active"] and row["signup_verified"] and row["published"]):
        raise HTTPException(status_code=404, detail="Studio storefront is not available.")


def _booking_request_key(
    email: str, property_address: str, package_id: int, scheduled_at: str, client_type: str
) -> str:
    """Give legacy form clients replay safety when they omit the hidden key."""
    identity = "\x1f".join(
        (
            tenant.get_studio_id(),
            email,
            property_address,
            str(package_id),
            scheduled_at,
            client_type,
        )
    )
    return f"legacy-{hashlib.sha256(identity.encode()).hexdigest()}"


def _referral_context(code: str) -> dict | None:
    if not code:
        return None
    row = db.one(
        """SELECT r.code, r.credit_cents
           FROM referral_codes r
          WHERE r.studio_id=?
            AND upper(r.code)=?
            AND r.active=1
            AND (r.max_uses IS NULL OR r.uses < r.max_uses)
          LIMIT 1""",
        (STUDIO_ID, code),
    )
    if not row:
        return None
    return {
        "code": row["code"],
        "credit_cents": int(row["credit_cents"] or 0),
        "credit_display": _money(int(row["credit_cents"] or 0)),
    }


def _book_context(
    error: str | None = None,
    thanks: bool = False,
    promo_code: str = "",
    request_key: str = "",
    returning_token: str = "",
):
    clean_code = _clean_booking_code(promo_code)
    profile = studio.get_profile()
    addons = studio.list_addons(active_only=True)
    twilight_addon = next((a for a in addons if a["slug"] == "twilight"), None)
    day_slots = scheduling.open_slots()
    twilight_only = scheduling.twilight_slots()
    slots = day_slots + twilight_only
    returning_client = None
    if returning_token:
        from .. import portal

        try:
            returning_client = portal.get_client_by_token(returning_token)
        except HTTPException:
            pass
    return {
        "profile": profile,
        "packages": studio.list_packages(active_only=True),
        "addons": addons,
        "twilight_addon": twilight_addon,
        "slots": slots,
        "day_slots": day_slots,
        "twilight_slots": twilight_only,
        "terms": commerce.BOOKING_TERMS.format(site_name=tenant.get_site_name()),
        "payments_on": stripe_checkout.payments_configured(),
        "error": error,
        "thanks": thanks,
        "promo_code": clean_code,
        "referral_context": _referral_context(clean_code),
        "returning_client": returning_client,
        "returning_token": returning_token if returning_client else "",
        "request_key": request_key or security.new_token(),
    }


def _credit_client_id(returning_token: str, email: str) -> int | None:
    """Resolve an account-credit capability without trusting email ownership alone."""
    token = returning_token.strip()
    if not token:
        return None
    from .. import portal

    try:
        client = portal.get_client_by_token(token)
    except HTTPException:
        return None
    stored_email = (client["email"] or "").strip().lower()
    return int(client["id"]) if stored_email == email else None


@router.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request):
    from .. import plan_limits

    if not (config.SAAS_MODE and tenant.get_studio_id() == "default"):
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "site/pricing.html",
        {
            "signup_enabled": config.SIGNUP_ENABLED,
            "invite_only": __import__(
                "eos.invites", fromlist=["invite_required"]
            ).invite_required(),
            "plans": plan_limits.LIMITS,
        },
    )


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
    _require_storefront_visible()
    profile = studio.get_profile()
    packages = studio.list_packages(active_only=True)
    return templates.TemplateResponse(
        request,
        "site/home.html",
        {"profile": profile, "packages": packages},
    )


@router.get("/book", response_class=HTMLResponse)
async def book_form(request: Request):
    _require_bookable()
    promo_code = request.query_params.get("ref") or request.query_params.get("promo_code") or ""
    return templates.TemplateResponse(
        request,
        "site/book.html",
        _book_context(
            promo_code=promo_code, returning_token=request.query_params.get("returning", "")
        ),
    )


@router.get("/book/embed", response_class=HTMLResponse)
async def book_embed_form(request: Request):
    """Chrome-free booking form for iframes on the studio's own website."""
    _require_bookable()
    promo_code = request.query_params.get("ref") or request.query_params.get("promo_code") or ""
    resp = templates.TemplateResponse(
        request,
        "site/book_embed.html",
        _book_context(
            promo_code=promo_code, returning_token=request.query_params.get("returning", "")
        ),
    )
    resp.headers["Content-Security-Policy"] = f"frame-ancestors {studio.embed_frame_ancestors()}"
    return resp


@router.get("/r/{code}")
async def referral_shortlink(request: Request, code: str):
    security.check_rate_limit(
        f"ref:{security.client_ip(request)}",
        config.RATE_LIMIT_PUBLIC_PER_MIN,
        detail="too many requests",
    )
    clean_code = _clean_booking_code(code)
    target = f"/book?ref={quote(clean_code)}" if clean_code else "/book"
    return RedirectResponse(target, status_code=303)


def _legal_context() -> dict:
    """Operator identity/contact for platform legal pages, with safe defaults."""
    contact = config.CONTACT_EMAIL or next(
        (e.strip() for e in config.PLATFORM_ADMIN_EMAILS.split(",") if e.strip()),
        "",
    )
    return {
        "operator_name": config.OPERATOR_NAME,
        "contact_email": contact,
        "base_domain": config.BASE_DOMAIN,
    }


@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse(request, "site/terms.html", _legal_context())


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(request, "site/privacy.html", _legal_context())


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
    request_key: str = Form(""),
    returning_token: str = Form(""),
    embed: str = Form(""),
):
    _require_bookable()
    is_embed = embed == "1"
    template = "site/book_embed.html" if is_embed else "site/book.html"
    ip = security.client_ip(request)
    if security.inquiry_throttled(ip, security.INQUIRY_BUCKET_BOOK):
        raise HTTPException(status_code=429, detail="too many requests")
    email = email.strip().lower()
    request_key = request_key.strip() or _booking_request_key(
        email, property_address.strip(), package_id, scheduled_at, "agent"
    )
    if not _EMAIL.match(email):
        return templates.TemplateResponse(
            request,
            template,
            _book_context(
                error="Invalid email.",
                promo_code=promo_code,
                request_key=request_key,
                returning_token=returning_token,
            ),
            status_code=400,
        )
    if not property_address.strip():
        return templates.TemplateResponse(
            request,
            template,
            _book_context(
                error="Property address is required.",
                promo_code=promo_code,
                request_key=request_key,
                returning_token=returning_token,
            ),
            status_code=400,
        )

    credit_client_id = _credit_client_id(returning_token, email)
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
            request_key=request_key,
            credit_client_id=credit_client_id,
        )
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else "Booking failed."
        return templates.TemplateResponse(
            request,
            template,
            _book_context(
                error=detail,
                promo_code=promo_code,
                request_key=request_key,
                returning_token=returning_token,
            ),
            status_code=e.status_code,
        )

    suffix = "?embed=1" if is_embed else ""
    if result["pay_slug"] and stripe_checkout.payments_configured():
        return RedirectResponse(f"/i/{result['pay_slug']}{suffix}", status_code=303)
    return RedirectResponse(f"/booking/{result['order_token']}{suffix}", status_code=303)


@router.get("/book/homeowner", response_class=HTMLResponse)
async def book_homeowner_form(request: Request):
    _require_bookable()
    ctx = _book_context()
    ctx["terms"] = commerce.BOOKING_TERMS.format(site_name=tenant.get_site_name())
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
    request_key: str = Form(""),
):
    _require_bookable()
    ip = security.client_ip(request)
    if security.inquiry_throttled(ip, security.INQUIRY_BUCKET_BOOK):
        raise HTTPException(status_code=429, detail="too many requests")
    email = email.strip().lower()
    request_key = request_key.strip() or _booking_request_key(
        email, property_address.strip(), package_id, scheduled_at, "homeowner"
    )
    if not _EMAIL.match(email):
        return templates.TemplateResponse(
            request,
            "site/book_homeowner.html",
            _book_context(error="Invalid email.", request_key=request_key),
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
            request_key=request_key,
        )
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else "Booking failed."
        return templates.TemplateResponse(
            request,
            "site/book_homeowner.html",
            _book_context(error=detail, request_key=request_key),
            status_code=e.status_code,
        )
    if result["pay_slug"] and stripe_checkout.payments_configured():
        return RedirectResponse(f"/i/{result['pay_slug']}", status_code=303)
    return RedirectResponse(f"/booking/{result['order_token']}", status_code=303)
