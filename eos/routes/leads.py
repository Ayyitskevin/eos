"""Studio admin leads inbox — buyer leads captured on property sites."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import leads, security
from ..render import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/listings/leads", response_class=HTMLResponse)
async def leads_inbox(request: Request):
    raw = request.query_params.get("listing", "")
    listing_id = int(raw) if raw.isdigit() else None
    return templates.TemplateResponse(
        request,
        "admin/leads.html",
        {
            "leads": leads.list_leads(listing_id=listing_id),
            "listing_filter": listing_id,
        },
    )


@router.get("/listings/leads.csv")
async def leads_csv_export():
    return Response(
        content=leads.leads_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="eos-leads.csv"'},
    )


@router.post("/listings/leads/{lead_id}/contacted")
async def lead_contacted(lead_id: int, contacted: str = Form("1")):
    leads.set_contacted(lead_id, contacted == "1")
    return RedirectResponse("/admin/listings/leads", status_code=303)
