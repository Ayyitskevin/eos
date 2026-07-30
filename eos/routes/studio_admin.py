import math

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import (
    acquisition,
    api_tokens,
    clients,
    commerce,
    config,
    db,
    delivery_notify,
    domain_verify,
    integration_events,
    jobs,
    payments,
    plan_limits,
    platform_billing,
    referrals,
    security,
    sms,
    studio,
    tenant,
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
    studio_row = studio.get_studio()
    domain_instr = None
    if studio_row and studio_row["custom_domain"]:
        domain_instr = domain_verify.verification_instructions(
            domain=studio_row["custom_domain"],
            slug=studio_row["slug"] or "",
            token=studio_row["domain_verify_token"] or "",
        )
    return templates.TemplateResponse(
        request,
        "admin/studio.html",
        {
            "new_api_token": getattr(request.state, "new_api_token", None),
            "new_webhook_secret": getattr(request.state, "new_webhook_secret", None),
            "studio": studio_row,
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
            "webhook_deliveries": webhooks.list_deliveries(),
            "delivery_notifications": delivery_notify.list_notifications(),
            "sms_reminders": sms.list_reminder_intents(),
            "failed_jobs": jobs.list_failed(),
            "pending_payment_holds": db.all_(
                """SELECT id, name, property_address, scheduled_at, payment_expires_at,
                          payment_reconcile_attempts, payment_reconcile_error
                   FROM inquiries WHERE studio_id=? AND status='pending_payment'
                   ORDER BY COALESCE(payment_expires_at, created_at), id LIMIT 50""",
                (str(STUDIO_ID),),
            ),
            "referrals": referrals.list_codes(),
            "referral_performance": acquisition.agent_referral_summary(),
            "client_list": clients.list_clients(),
            "webhook_events": webhooks.EVENTS,
            "signup_enabled": config.SIGNUP_ENABLED,
            "base_domain": config.BASE_DOMAIN,
            "google_configured": google_calendar.is_configured(),
            "google_connected": google_calendar.is_connected(),
            "google_sync_intents": google_calendar.list_sync_intents(),
            "dropbox_configured": dropbox.is_configured(),
            "dropbox_connected": dropbox.is_connected(),
            "billing_configured": platform_billing.is_configured(),
            "billing": platform_billing.studio_billing(),
            "usage": usage.snapshot(),
            "plan_limits": plan_limits.limits_for(),
            "payments": payments.connect_status(),
            "domain_instructions": domain_instr,
            "integration_events": integration_events.list_recent(limit=15),
            "dropbox_log": dropbox.list_ingest_logs(),
            "embed_url": f"{tenant.get_base_url()}/book/embed",
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
    analytics_digest_enabled: bool = Form(False),
    lead_capture_enabled: bool = Form(False),
    embed_allowed_domains: str = Form("*"),
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
        analytics_digest_enabled=analytics_digest_enabled,
        lead_capture_enabled=lead_capture_enabled,
        embed_allowed_domains=embed_allowed_domains.strip() or "*",
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
        domain_verify.save_pending_domain(domain)
    else:
        domain_verify.save_pending_domain("")
    return RedirectResponse("/admin/studio#domain", status_code=303)


@router.post("/studio/domain/verify")
async def studio_domain_verify():
    plan_limits.check_custom_domain()
    ok, msg = domain_verify.try_verify_saved()
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return RedirectResponse("/admin/studio#domain", status_code=303)


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
    if not name.strip():
        raise HTTPException(status_code=400, detail="package name is required")
    if not math.isfinite(price_dollars) or not 0 <= price_dollars <= 100000:
        raise HTTPException(status_code=400, detail="package price must be between $0 and $100,000")
    if not math.isfinite(deposit_dollars) or not 0 <= deposit_dollars <= price_dollars:
        raise HTTPException(
            status_code=400, detail="deposit must be between $0 and the package price"
        )
    if not 1 <= turnaround_hours <= 8760:
        raise HTTPException(status_code=400, detail="turnaround must be between 1 and 8,760 hours")
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
async def create_api_token(request: Request, label: str = Form("Zapier")):
    _tid, raw = api_tokens.create_token(label=label)
    request.state.new_api_token = raw
    response = await studio_settings(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/studio/api-tokens/{token_id}/revoke")
async def revoke_api_token(token_id: int):
    api_tokens.revoke_token(token_id)
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/studio/webhooks")
async def create_webhook(
    request: Request,
    label: str = Form("Zapier"),
    url: str = Form(...),
    events: list[str] = Form(default=[]),
):
    try:
        hook_id = webhooks.create_subscription(label=label, url=url, events=events)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    created = db.one(
        "SELECT secret FROM webhook_subscriptions WHERE id=? AND studio_id=?",
        (hook_id, str(STUDIO_ID)),
    )
    if not created:
        raise HTTPException(status_code=500, detail="webhook creation could not be verified")
    request.state.new_webhook_secret = created["secret"]
    response = await studio_settings(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/studio/webhooks/{hook_id}/delete")
async def delete_webhook(hook_id: int):
    webhooks.delete_subscription(hook_id)
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/studio/webhook-deliveries/{delivery_id}/retry")
async def retry_webhook_delivery(delivery_id: int):
    if not webhooks.retry_delivery(delivery_id):
        raise HTTPException(
            status_code=409,
            detail="only definite failed webhook deliveries can be retried; reconcile unknown outcomes first",
        )
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/studio/webhook-deliveries/{delivery_id}/reconcile")
async def reconcile_webhook_delivery(delivery_id: int, outcome: str = Form(...)):
    if outcome not in {"delivered", "not_delivered"}:
        raise HTTPException(status_code=400, detail="invalid webhook reconciliation outcome")
    try:
        webhooks.reconcile_delivery(delivery_id, delivered=outcome == "delivered")
    except webhooks.WebhookDeliveryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except webhooks.WebhookDeliveryReconciliationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/studio/delivery-notifications/{notification_id}/retry")
async def retry_delivery_notification(notification_id: int):
    if not delivery_notify.retry_notification(notification_id):
        raise HTTPException(
            status_code=409,
            detail="only definite failed notifications can be retried; reconcile unknown outcomes first",
        )
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/studio/delivery-notifications/{notification_id}/reconcile")
async def reconcile_delivery_notification(notification_id: int, outcome: str = Form(...)):
    if outcome not in {"delivered", "not_delivered"}:
        raise HTTPException(status_code=400, detail="invalid email reconciliation outcome")
    try:
        delivery_notify.reconcile_notification(
            notification_id,
            delivered=outcome == "delivered",
        )
    except delivery_notify.DeliveryNotificationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except delivery_notify.DeliveryNotificationReconciliationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/studio/sms-reminders/{intent_id}/retry")
async def retry_sms_reminder(intent_id: int):
    if not sms.retry_reminder(intent_id):
        raise HTTPException(
            status_code=409,
            detail="only definite failed SMS reminders can be retried; reconcile unknown outcomes first",
        )
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/studio/sms-reminders/{intent_id}/reconcile")
async def reconcile_sms_reminder(intent_id: int, outcome: str = Form(...)):
    if outcome not in {"delivered", "not_delivered"}:
        raise HTTPException(status_code=400, detail="invalid SMS reconciliation outcome")
    try:
        sms.reconcile_reminder(intent_id, delivered=outcome == "delivered")
    except sms.SmsReminderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except sms.SmsReminderReconciliationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/studio/jobs/{job_id}/retry")
async def retry_background_job(job_id: int):
    if not jobs.retry_job(job_id):
        raise HTTPException(status_code=404, detail="failed background job not found")
    return RedirectResponse("/admin/studio#operations", status_code=303)


@router.post("/studio/google-intents/{appointment_id}/reconcile")
async def reconcile_google_intent(
    appointment_id: int,
    outcome: str = Form(...),
):
    if outcome not in {"applied", "not_applied"}:
        raise HTTPException(status_code=400, detail="invalid Google reconciliation outcome")
    try:
        google_calendar.reconcile_unknown(
            appointment_id,
            applied=outcome == "applied",
        )
    except google_calendar.GoogleIntentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except google_calendar.GoogleReconciliationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/admin/studio#google-calendar", status_code=303)


@router.post("/studio/pending-bookings/expire")
async def expire_pending_booking_holds():
    count = commerce.expire_pending_bookings()
    return RedirectResponse(f"/admin/studio?expired_holds={count}#operations", status_code=303)


@router.post("/studio/referrals")
async def create_referral(
    code: str = Form(...),
    credit_dollars: float = Form(25),
    max_uses: str = Form(""),
    referrer_client_id: str = Form(""),
):
    if not code.strip():
        raise HTTPException(status_code=400, detail="referral code is required")
    if not math.isfinite(credit_dollars) or not 0 <= credit_dollars <= 10000:
        raise HTTPException(
            status_code=400, detail="referral credit must be between $0 and $10,000"
        )
    max_raw = max_uses.strip()
    try:
        max_u = int(max_raw) if max_raw else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid max uses") from exc
    if max_u is not None and not 1 <= max_u <= 1000000:
        raise HTTPException(status_code=400, detail="max uses must be between 1 and 1,000,000")
    referrer_raw = referrer_client_id.strip()
    try:
        referrer_id = int(referrer_raw) if referrer_raw else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid referrer") from exc
    referrals.create_code(
        code=code,
        credit_cents=round(credit_dollars * 100),
        referrer_client_id=referrer_id,
        max_uses=max_u,
    )
    return RedirectResponse("/admin/studio#integrations", status_code=303)
