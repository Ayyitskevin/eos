"""Public agent portal — deliveries, reschedule, brokerage view."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, portal, reschedule, scheduling, studio
from .. import brokerage_portal as bp
from ..render import templates

router = APIRouter()


@router.get("/portal/{token}", response_class=HTMLResponse)
async def agent_portal(request: Request, token: str):
    client = portal.get_client_by_token(token)
    rows = portal.deliveries(client["id"])
    upcoming = reschedule.upcoming_for_client(client["id"])
    return templates.TemplateResponse(
        request, "public/portal.html",
        {
            "client": client,
            "deliveries": rows,
            "upcoming": upcoming,
            "portal_token": token,
            "base_url": config.BASE_URL,
            "payments_on": bool(config.STRIPE_SECRET_KEY),
            "upsell": studio.delivery_upsell(),
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
        request, "public/reschedule.html",
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
    client = bp.get_brokerage_by_token(token)
    data = bp.portal_summary(client["id"])
    return templates.TemplateResponse(
        request, "public/brokerage_portal.html",
        {
            "client": client,
            "totals": data["totals"],
            "statement_rows": data["statement_rows"],
            "deliveries": data["deliveries"],
            "base_url": config.BASE_URL,
        },
    )