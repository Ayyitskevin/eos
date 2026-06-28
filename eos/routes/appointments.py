from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import appointments, calendar_view, clients, listings, security, users
from ..appointments import KINDS, STATUSES
from ..render import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


def _calendar_ctx(request: Request, *, view: str, date: str, photographer: str, edit_id: str):
    anchor = calendar_view.parse_anchor(date)
    if view == "week":
        range_start, range_end = calendar_view.week_bounds(anchor)
        grid_days = calendar_view.week_days(anchor)
        month_grid = None
    else:
        view = "month"
        range_start, range_end = calendar_view.month_bounds(anchor)
        grid_days = None
        month_grid = calendar_view.month_grid(anchor.year, anchor.month)
    photographer_id = int(photographer) if photographer.strip().isdigit() else None
    events = calendar_view.list_events(
        range_start=range_start,
        range_end=range_end,
        photographer_id=photographer_id,
    )
    busy = calendar_view.busy_blocks(range_start=range_start, range_end=range_end)
    by_day = calendar_view.events_by_day(events)
    busy_by_day: dict[str, list] = {}
    for b in busy:
        busy_by_day.setdefault(b["day"], []).append(b)
    edit_appt = None
    if edit_id.isdigit():
        try:
            edit_appt = appointments.get_appointment(int(edit_id))
        except HTTPException:
            edit_appt = None
    return {
        "view": view,
        "anchor": anchor,
        "nav": calendar_view.nav_dates(anchor, view=view),
        "range_start": range_start,
        "range_end": range_end,
        "month_grid": month_grid,
        "grid_days": grid_days,
        "events": events,
        "by_day": by_day,
        "busy": busy,
        "busy_by_day": busy_by_day,
        "photographer_id": photographer_id,
        "edit_appt": edit_appt,
        "appointments": appointments.list_upcoming(days=30),
        "listings": listings.list_listings(),
        "client_list": clients.list_clients(),
        "kinds": KINDS,
        "statuses": STATUSES,
        "operators": users.list_users(),
        "google_connected": __import__(
            "eos.integrations.google_calendar",
            fromlist=["is_enabled"],
        ).is_enabled(),
    }


@router.get("/calendar", response_class=HTMLResponse)
async def calendar(
    request: Request,
    view: str = "month",
    date: str = "",
    photographer: str = "",
    edit: str = "",
):
    ctx = _calendar_ctx(request, view=view, date=date, photographer=photographer, edit_id=edit)
    return templates.TemplateResponse(request, "admin/calendar.html", ctx)


@router.post("/appointments")
async def create_appt(
    title: str = Form(...),
    kind: str = Form("shoot"),
    starts_at: str = Form(""),
    location: str = Form(""),
    listing_id: str = Form(""),
    client_id: str = Form(""),
    assigned_user_id: str = Form(""),
    view: str = Form("month"),
    date: str = Form(""),
    photographer: str = Form(""),
):
    lid = int(listing_id) if listing_id.strip().isdigit() else None
    cid = int(client_id) if client_id.strip().isdigit() else None
    uid = int(assigned_user_id) if assigned_user_id.strip().isdigit() else None
    appointments.create_appointment(
        title,
        kind=kind,
        starts_at=starts_at.strip() or None,
        location=location,
        listing_id=lid,
        client_id=cid,
        assigned_user_id=uid,
    )
    q = f"view={view}&date={date}&photographer={photographer}"
    return RedirectResponse(f"/admin/calendar?{q}", status_code=303)


@router.post("/appointments/{appt_id}")
async def update_appt(
    appt_id: int,
    title: str = Form(...),
    kind: str = Form("shoot"),
    status: str = Form("proposed"),
    starts_at: str = Form(""),
    location: str = Form(""),
    assigned_user_id: str = Form(""),
    listing_id: str = Form(""),
    client_id: str = Form(""),
    view: str = Form("month"),
    date: str = Form(""),
    photographer: str = Form(""),
):
    uid = int(assigned_user_id) if assigned_user_id.strip().isdigit() else None
    lid = int(listing_id) if listing_id.strip().isdigit() else None
    cid = int(client_id) if client_id.strip().isdigit() else None
    starts = starts_at.strip() or None
    if starts:
        normalized = starts.replace("T", " ")
        if len(normalized) == 16:
            normalized += ":00"
        appointments.reschedule_appointment(appt_id, starts_at=normalized)
    appointments.update_appointment(
        appt_id,
        title=title,
        kind=kind,
        status=status,
        location=location,
        assigned_user_id=uid,
        listing_id=lid,
        client_id=cid,
    )
    q = f"view={view}&date={date}&photographer={photographer}&edit={appt_id}"
    return RedirectResponse(f"/admin/calendar?{q}", status_code=303)


@router.post("/appointments/{appt_id}/reschedule")
async def reschedule_appt(
    appt_id: int,
    starts_at: str = Form(...),
    view: str = Form("month"),
    date: str = Form(""),
    photographer: str = Form(""),
):
    normalized = starts_at.strip().replace("T", " ")
    if len(normalized) == 16:
        normalized += ":00"
    appointments.reschedule_appointment(appt_id, starts_at=normalized)
    q = f"view={view}&date={date}&photographer={photographer}&edit={appt_id}"
    return RedirectResponse(f"/admin/calendar?{q}", status_code=303)
