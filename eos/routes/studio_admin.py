from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import (
    api_tokens,
    config,
    db,
    integration_events,
    plan_limits,
    platform_billing,
    referrals,
    security,
    studio,
    usage,
    users,
    webhooks,
)
from ..integrations import dropbox, google_calendar
from ..render import templates
from ..vocab import STUDIO_ID

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/studio", response_class=HTMLResponse)
async def studio_settings(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/studio.html",
        {
            "studio": studio.get_studio(),
            "profile": studio.get_profile(),
            "packages": studio.list_packages(),
            "presets": studio.list_crop_presets(),
            "inquiries": studio.list_inquiries(20),
            "addons": studio.list_addons(),
            "promos": studio.list_promo_codes(),
            "operators": users.list_users(),
            "saas_mode": config.SAAS_MODE or bool(users.list_users()),
            "api_tokens": api_tokens.list_tokens(),
            "webhooks": webhooks.list_subscriptions(),
            "referrals": referrals.list_codes(),
            "webhook_events": webhooks.EVENTS,
            "signup_enabled": config.SIGNUP_ENABLED,
            "base_domain": config.BASE_DOMAIN,
            "google_configured": google_calendar.is_configured(),
            "google_connected": google_calendar.is_connected(),
            "dropbox_configured": dropbox.is_configured(),
            "dropbox_connected": dropbox.is_connected(),
            "billing_configured": platform_billing.is_configured(),
            "billing": platform_billing.studio_billing(),
            "usage": usage.snapshot(),
            "plan_limits": plan_limits.limits_for(),
            "integration_events": integration_events.list_recent(15),
            "dropbox_log": db.all_(
                """SELECT * FROM dropbox_ingest_log WHERE studio_id=?
                   ORDER BY created_at DESC LIMIT 15""",
                (str(STUDIO_ID),),
            )
            if dropbox.is_connected()
            else [],
        },
    )


@router.post("/studio/users")
async def add_user(
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(""),
    role: str = Form("operator"),
):
    users.create_user(email, password, name=name, role=role)
    return RedirectResponse("/admin/studio", status_code=303)


@router.post("/studio")
async def studio_update(
    name: str = Form(...),
    contact_email: str = Form(""),
    headline: str = Form(""),
    about: str = Form(""),
    service_area: str = Form(""),
    published: bool = Form(False),
    booking_enabled: bool = Form(False),
    min_notice_hours: int = Form(24),
    buffer_minutes: int = Form(30),
    slot_minutes: int = Form(90),
    day_start_min: int = Form(480),
    day_end_min: int = Form(1080),
    book_weekdays: str = Form("0,1,2,3,4,5"),
    pay_to_download: bool = Form(False),
    watermark_until_paid: bool = Form(False),
    auto_deliver_email: bool = Form(False),
    auto_publish_site: bool = Form(False),
    twilight_start_min: int = Form(1020),
    twilight_end_min: int = Form(1140),
    delivery_upsell_title: str = Form(""),
    delivery_upsell_body: str = Form(""),
    delivery_upsell_link: str = Form("/book"),
    drive_time_enabled: bool = Form(False),
    drive_buffer_min: int = Form(30),
):
    studio.update_studio(name=name, contact_email=contact_email)
    studio.update_profile(
        headline=headline,
        about=about,
        service_area=service_area,
        published=published,
        booking_enabled=booking_enabled,
        min_notice_hours=min_notice_hours,
        buffer_minutes=buffer_minutes,
        slot_minutes=slot_minutes,
        day_start_min=day_start_min,
        day_end_min=day_end_min,
        book_weekdays=book_weekdays.strip(),
        pay_to_download=pay_to_download,
        watermark_until_paid=watermark_until_paid,
        auto_deliver_email=auto_deliver_email,
        auto_publish_site=auto_publish_site,
        twilight_start_min=twilight_start_min,
        twilight_end_min=twilight_end_min,
        delivery_upsell_title=delivery_upsell_title.strip(),
        delivery_upsell_body=delivery_upsell_body.strip(),
        delivery_upsell_link=delivery_upsell_link.strip() or "/book",
        drive_time_enabled=drive_time_enabled,
        drive_buffer_min=drive_buffer_min,
    )
    return RedirectResponse("/admin/studio", status_code=303)


@router.post("/studio/domain")
async def studio_domain(custom_domain: str = Form("")):
    domain = custom_domain.strip().lower()
    if domain:
        plan_limits.check_custom_domain()
        if "://" in domain or "/" in domain:
            raise HTTPException(
                status_code=400, detail="Enter hostname only, e.g. photos.yourstudio.com"
            )
    studio.update_studio(
        custom_domain=domain or None,
        custom_domain_verified=1 if domain else 0,
    )
    return RedirectResponse("/admin/studio", status_code=303)


@router.post("/studio/packages/{package_id}")
async def package_update(
    package_id: int,
    name: str = Form(...),
    description: str = Form(""),
    price_dollars: float = Form(...),
    deposit_dollars: float = Form(0),
    turnaround_hours: int = Form(24),
    active: bool = Form(False),
):
    studio.update_package(
        package_id,
        name=name,
        description=description,
        price_cents=round(price_dollars * 100),
        deposit_cents=round(deposit_dollars * 100),
        turnaround_hours=turnaround_hours,
        active=active,
    )
    return RedirectResponse("/admin/studio", status_code=303)


@router.post("/studio/api-tokens")
async def create_api_token(label: str = Form("Zapier")):
    _tid, raw = api_tokens.create_token(label=label)
    return RedirectResponse(f"/admin/studio?token={raw}", status_code=303)


@router.post("/studio/api-tokens/{token_id}/revoke")
async def revoke_api_token(token_id: int):
    api_tokens.revoke_token(token_id)
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/studio/webhooks")
async def create_webhook(
    label: str = Form("Zapier"),
    url: str = Form(...),
    events: list[str] = Form(default=[]),
):
    try:
        webhooks.create_subscription(label=label, url=url, events=events)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/studio/webhooks/{hook_id}/delete")
async def delete_webhook(hook_id: int):
    webhooks.delete_subscription(hook_id)
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/studio/referrals")
async def create_referral(
    code: str = Form(...),
    credit_dollars: float = Form(25),
    max_uses: str = Form(""),
):
    max_u = int(max_uses) if max_uses.strip().isdigit() else None
    referrals.create_code(
        code=code,
        credit_cents=round(credit_dollars * 100),
        max_uses=max_u,
    )
    return RedirectResponse("/admin/studio#integrations", status_code=303)
