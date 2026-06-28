from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import brand_kits, clients, security
from ..render import templates
from ..vocab import CLIENT_TYPES

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/clients", response_class=HTMLResponse)
async def clients_index(request: Request):
    return templates.TemplateResponse(
        request, "admin/clients.html",
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
        name, client_type=client_type, company=company, email=email,
        phone=phone, license_number=license_number, parent_id=pid,
    )
    return RedirectResponse(f"/admin/clients/{cid}", status_code=303)


@router.get("/clients/{client_id}", response_class=HTMLResponse)
async def client_detail(request: Request, client_id: int):
    c = clients.get_client(client_id)
    from .. import db
    kids = db.all_("SELECT * FROM clients WHERE parent_id=? ORDER BY name", (client_id,))
    listings_rows = db.all_(
        "SELECT * FROM listings WHERE client_id=? ORDER BY created_at DESC",
        (client_id,),
    )
    return templates.TemplateResponse(
        request, "admin/client.html",
        {
            "c": c,
            "children": kids,
            "listings": listings_rows,
            "all_clients": clients.list_clients(),
            "client_types": CLIENT_TYPES,
            "brand_kit": brand_kits.get_kit(client_id),
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
        client_id, name=name, client_type=client_type, company=company,
        email=email, phone=phone, license_number=license_number,
        notes=notes, parent_id=pid,
    )
    return RedirectResponse(f"/admin/clients/{client_id}", status_code=303)