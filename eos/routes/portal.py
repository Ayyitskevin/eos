"""Public agent portal — deliveries, reschedule, brokerage view."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import (
    analytics,
    config,
    portal,
    reschedule,
    scheduling,
    security,
    stripe_checkout,
    studio,
    tenant,
    video_render,
)
from .. import brokerage_portal as bp
from ..render import templates

router = APIRouter()


def _check_public_rate(request: Request) -> None:
    """Burst-cap unauthenticated magic-link views (per client IP)."""
    security.check_rate_limit(
        f"portal:{security.client_ip(request)}",
        config.RATE_LIMIT_PUBLIC_PER_MIN,
        detail="too many requests",
    )


@router.get("/portal/{token}", response_class=HTMLResponse)
async def agent_portal(request: Request, token: str):
    _check_public_rate(request)
    client = portal.get_client_by_token(token)
    rows = portal.deliveries(client["id"])
    upcoming = reschedule.upcoming_for_client(client["id"])
    repeat = portal.repeat_links(client["id"], portal_token=token)
    view_counts = analytics.portal_counts(
        [row["listing_id"] for row in rows], studio_id=tenant.get_studio_id()
    )
    return templates.TemplateResponse(
        request,
        "public/portal.html",
        {
            "client": client,
            "deliveries": rows,
            "view_counts": view_counts,
            "upcoming": upcoming,
            "portal_token": token,
            "base_url": tenant.get_base_url(),
            "payments_on": stripe_checkout.payments_configured(),
            "upsell": studio.delivery_upsell(),
            "repeat": repeat,
            "video_galleries": video_render.galleries_with_ready_videos(
                [row["gallery_id"] for row in rows]
            ),
            "rescheduled": request.query_params.get("rescheduled"),
        },
    )


@router.get("/portal/{token}/reschedule/{appointment_id}", response_class=HTMLResponse)
async def reschedule_form(request: Request, token: str, appointment_id: int):
    client = portal.get_client_by_token(token)
    appt = reschedule.upcoming_for_client(client["id"])
    match = next((a for a in appt if a["id"] == appointment_id), None)
    if not match:
        return RedirectResponse(f"/portal/{token}", status_code=303)
    slots = scheduling.reschedule_slots(days=14)
    return templates.TemplateResponse(
        request,
        "public/reschedule.html",
        {
            "client": client,
            "appointment": match,
            "slots": slots,
            "portal_token": token,
        },
    )


@router.post("/portal/{token}/reschedule/{appointment_id}")
async def reschedule_submit(
    token: str,
    appointment_id: int,
    scheduled_at: str = Form(...),
):
    client = portal.get_client_by_token(token)
    hold_token = reschedule.create_hold(
        appointment_id=appointment_id,
        client_id=client["id"],
        starts_at=scheduled_at,
    )
    reschedule.confirm_hold(hold_token, client_id=client["id"])
    return RedirectResponse(f"/portal/{token}?rescheduled=1", status_code=303)


@router.get("/portal/brokerage/{token}", response_class=HTMLResponse)
async def brokerage_portal_view(request: Request, token: str):
    _check_public_rate(request)
    client = bp.get_brokerage_by_token(token)
    data = bp.portal_summary(client["id"])
    return templates.TemplateResponse(
        request,
        "public/brokerage_portal.html",
        {
            "client": client,
            "totals": data["totals"],
            "open_invoices": data["open_invoices"],
            "paid_invoices": data["paid_invoices"],
            "agent_activity": data["agent_activity"],
            "deliveries": data["deliveries"],
            "base_url": tenant.get_base_url(),
            "payments_on": stripe_checkout.payments_configured(),
        },
    )
