"""Stripe Connect onboarding — studios collect client payments."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import payments, security, stripe_connect, tenant
from ..render import templates

router = APIRouter(prefix="/admin/stripe", dependencies=[Depends(security.require_admin)])


@router.get("/connect", response_class=HTMLResponse)
async def connect_settings(request: Request):
    status = payments.connect_status()
    return templates.TemplateResponse(
        request,
        "admin/stripe_connect.html",
        {
            "status": status,
            "payments_on": payments.configured(),
            "base_url": tenant.get_base_url(),
        },
    )


@router.post("/connect/start")
async def connect_start():
    if not stripe_connect.is_configured():
        return RedirectResponse("/admin/stripe/connect?error=not_configured", status_code=303)
    base = tenant.get_base_url()
    url = stripe_connect.onboarding_url(
        refresh_url=f"{base}/admin/stripe/connect?refresh=1",
        return_url=f"{base}/admin/stripe/connect?thanks=1",
    )
    return RedirectResponse(url, status_code=303)


@router.post("/connect/refresh")
async def connect_refresh():
    stripe_connect.refresh_account_status()
    return RedirectResponse("/admin/stripe/connect", status_code=303)


@router.post("/connect/dashboard")
async def connect_dashboard():
    url = stripe_connect.dashboard_url()
    return RedirectResponse(url, status_code=303)


@router.post("/connect/disconnect")
async def connect_disconnect():
    stripe_connect.disconnect()
    return RedirectResponse("/admin/stripe/connect", status_code=303)