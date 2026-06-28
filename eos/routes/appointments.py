from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import appointments, clients, listings, security
from ..appointments import KINDS, STATUSES
from ..render import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/calendar", response_class=HTMLResponse)
async def calendar(request: Request):
    return templates.TemplateResponse(
        request, "admin/calendar.html",
        {
            "appointments": appointments.list_upcoming(days=30),
            "listings": listings.list_listings(),
            "client_list": clients.list_clients(),
            "kinds": KINDS,
            "statuses": STATUSES,
        },
    )


@router.post("/appointments")
async def create_appt(
    title: str = Form(...),
    kind: str = Form("shoot"),
    starts_at: str = Form(""),
    location: str = Form(""),
    listing_id: str = Form(""),
    client_id: str = Form(""),
):
    lid = int(listing_id) if listing_id.strip().isdigit() else None
    cid = int(client_id) if client_id.strip().isdigit() else None
    aid = appointments.create_appointment(
        title, kind=kind, starts_at=starts_at.strip() or None,
        location=location, listing_id=lid, client_id=cid,
    )
    return RedirectResponse(f"/admin/calendar", status_code=303)


@router.post("/appointments/{appt_id}")
async def update_appt(
    appt_id: int,
    title: str = Form(...),
    kind: str = Form("shoot"),
    status: str = Form("proposed"),
    starts_at: str = Form(""),
    location: str = Form(""),
):
    appointments.update_appointment(
        appt_id, title=title, kind=kind, status=status,
        starts_at=starts_at.strip() or None, location=location,
    )
    return RedirectResponse("/admin/calendar", status_code=303)