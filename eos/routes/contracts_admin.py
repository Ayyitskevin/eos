from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, contracts, listings, security
from ..render import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/contracts/{contract_id}", response_class=HTMLResponse)
async def contract_detail(request: Request, contract_id: int):
    d = contracts.get_contract(contract_id)
    listing = listings.get_listing(d["listing_id"])
    client = None
    if listing["client_id"]:
        from .. import db

        client = db.one("SELECT * FROM clients WHERE id=?", (listing["client_id"],))
    return templates.TemplateResponse(
        request,
        "admin/contract.html",
        {
            "d": d,
            "listing": listing,
            "client": client,
            "base_url": config.BASE_URL,
            "mailer_on": __import__("eos.mailer", fromlist=["configured"]).configured(),
        },
    )


@router.post("/listings/{listing_id}/contracts")
async def create_contract(listing_id: int):
    listings.get_listing(listing_id)
    cid = contracts.create_contract(listing_id)
    return RedirectResponse(f"/admin/contracts/{cid}", status_code=303)


@router.post("/contracts/{contract_id}")
async def update_contract(contract_id: int, title: str = Form(...), body: str = Form(...)):
    contracts.update_contract(contract_id, title=title, body=body)
    return RedirectResponse(f"/admin/contracts/{contract_id}", status_code=303)


@router.post("/contracts/{contract_id}/send")
async def send_contract(contract_id: int):
    contracts.mark_sent(contract_id)
    return RedirectResponse(f"/admin/contracts/{contract_id}", status_code=303)
