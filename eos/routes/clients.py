from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import brand_kits, churn, clients, mailer, portal, security
from .. import rebooking as rebooking_email
from ..render import templates
from ..vocab import CLIENT_TYPE_LABELS, CLIENT_TYPES, STUDIO_ID

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/clients", response_class=HTMLResponse)
async def clients_index(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/clients.html",
        {"clients": clients.list_clients(), "client_types": CLIENT_TYPES},
    )


@router.post("/clients")
async def clients_create(
    name: str = Form(...),
    client_type: str = Form("agent"),
    company: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    license_number: str = Form(""),
    parent_id: str = Form(""),
):
    pid = int(parent_id) if parent_id.strip().isdigit() else None
    cid = clients.create_client(
        name,
        client_type=client_type,
        company=company,
        email=email,
        phone=phone,
        license_number=license_number,
        parent_id=pid,
    )
    return RedirectResponse(f"/admin/clients/{cid}", status_code=303)


@router.get("/clients/{client_id}", response_class=HTMLResponse)
async def client_detail(request: Request, client_id: int):
    c = clients.get_client(client_id)
    from .. import db

    kids = db.all_(
        "SELECT * FROM clients WHERE parent_id=? AND studio_id=? ORDER BY name",
        (client_id, STUDIO_ID),
    )
    listings_rows = db.all_(
        "SELECT * FROM listings WHERE client_id=? AND studio_id=? ORDER BY created_at DESC",
        (client_id, STUDIO_ID),
    )
    portal_link = portal.portal_url(client_id) if c["email"] else None
    brokerage_link = None
    if c["client_type"] == "brokerage":
        brokerage_link = portal.brokerage_portal_url(client_id)
    from .. import credits

    rebooking_opportunity = churn.rebooking_for_client(client_id)
    if rebooking_opportunity:
        rebooking_opportunity = rebooking_email.decorate_opportunity(rebooking_opportunity)
    rebooking_notice = request.query_params.get("rebooking")
    rebooking_draft = None
    if rebooking_notice == "draft":
        try:
            rebooking_draft = rebooking_email.build_email(client_id)
        except HTTPException:
            rebooking_draft = None

    return templates.TemplateResponse(
        request,
        "admin/client.html",
        {
            "c": c,
            "credit_balance": credits.balance(client_id),
            "credit_ledger": credits.ledger(client_id),
            "brokerage_link": brokerage_link,
            "children": kids,
            "listings": listings_rows,
            "all_clients": clients.list_clients(),
            "client_types": CLIENT_TYPES,
            "brand_kit": brand_kits.get_kit(client_id),
            "portal_link": portal_link,
            "client_type_labels": CLIENT_TYPE_LABELS,
            "rebooking": rebooking_opportunity,
            "rebooking_draft": rebooking_draft,
            "rebooking_notice": rebooking_notice,
            "rebooking_mailer_on": mailer.configured(),
        },
    )


@router.post("/clients/{client_id}")
async def client_update(
    client_id: int,
    name: str = Form(...),
    client_type: str = Form("agent"),
    company: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    license_number: str = Form(""),
    notes: str = Form(""),
    parent_id: str = Form(""),
):
    pid = int(parent_id) if parent_id.strip().isdigit() else None
    if pid == client_id:
        pid = None
    clients.update_client(
        client_id,
        name=name,
        client_type=client_type,
        company=company,
        email=email,
        phone=phone,
        license_number=license_number,
        notes=notes,
        parent_id=pid,
    )
    return RedirectResponse(f"/admin/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/credit")
async def client_add_credit(
    client_id: int,
    amount_dollars: float = Form(...),
    note: str = Form(""),
):
    from .. import credits

    credits.add_credit(client_id, amount_cents=round(amount_dollars * 100), note=note)
    return RedirectResponse(f"/admin/clients/{client_id}", status_code=303)
