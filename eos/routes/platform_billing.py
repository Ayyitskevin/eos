"""Admin billing — per-studio Stripe subscriptions."""

import logging

import stripe
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, plan_limits, platform_billing, security, usage
from ..render import templates

log = logging.getLogger("eos.routes.platform_billing")
router = APIRouter()


@router.get("/admin/billing", response_class=HTMLResponse)
async def billing_page(request: Request, _: None = Depends(security.require_admin)):
    return templates.TemplateResponse(
        request,
        "admin/billing.html",
        {
            "billing": platform_billing.studio_billing(),
            "plans": platform_billing.PLANS,
            "configured": platform_billing.is_configured(),
            "thanks": request.query_params.get("thanks"),
            "usage": usage.snapshot(),
            "plan_limits": plan_limits.limits_for(),
        },
    )


@router.post("/admin/billing/checkout")
async def billing_checkout(plan: str = Form(...), _: None = Depends(security.require_admin)):
    try:
        url = platform_billing.create_checkout(plan)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return RedirectResponse(url, status_code=303)


@router.post("/admin/billing/portal")
async def billing_portal(_: None = Depends(security.require_admin)):
    try:
        url = platform_billing.create_portal()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return RedirectResponse(url, status_code=303)


@router.post("/stripe/platform/webhook")
async def platform_webhook(request: Request):
    if not config.STRIPE_PLATFORM_WEBHOOK_SECRET:
        raise HTTPException(status_code=503)
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig,
            config.STRIPE_PLATFORM_WEBHOOK_SECRET,
        )
    except Exception:
        raise HTTPException(status_code=400)
    platform_billing.handle_webhook_event(event)
    log.info("platform webhook %s", event["type"])
    return {"ok": True}
