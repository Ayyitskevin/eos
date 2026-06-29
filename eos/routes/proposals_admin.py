import json
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import clients, config, listings, proposals, security
from ..render import templates

log = logging.getLogger("eos.routes.proposals_admin")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/proposals/{proposal_id}", response_class=HTMLResponse)
async def proposal_detail(request: Request, proposal_id: int):
    prop = proposals.get_proposal(proposal_id)
    listing = listings.get_listing(prop["listing_id"])
    client = None
    if listing["client_id"]:
        client = clients.get_client(listing["client_id"])
    return templates.TemplateResponse(
        request,
        "admin/proposal.html",
        {
            "d": prop,
            "listing": listing,
            "client": client,
            "items": json.loads(prop["line_items"] or "[]"),
            "presets": proposals.package_presets(),
            "base_url": config.BASE_URL,
            "mailer_on": __import__("eos.mailer", fromlist=["configured"]).configured(),
        },
    )


@router.post("/listings/{listing_id}/proposals")
async def create_proposal(listing_id: int, preset: str = Form("blank")):
    listings.get_listing(listing_id)
    pid = proposals.create_proposal(listing_id, preset=preset)
    return RedirectResponse(f"/admin/proposals/{pid}", status_code=303)


@router.post("/proposals/{proposal_id}")
async def update_proposal(
    request: Request, proposal_id: int, title: str = Form(...), intro: str = Form("")
):
    form = await request.form()
    line_items, total = proposals.parse_items(form)
    proposals.update_proposal(
        proposal_id, title=title, intro=intro, line_items=line_items, total_cents=total
    )
    return RedirectResponse(f"/admin/proposals/{proposal_id}", status_code=303)


@router.post("/proposals/{proposal_id}/send")
async def send_proposal(proposal_id: int):
    proposals.mark_sent(proposal_id)
    return RedirectResponse(f"/admin/proposals/{proposal_id}", status_code=303)
