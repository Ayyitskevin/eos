"""Public proposals /p/{slug} and contracts /c/{slug}."""

import json
import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import contracts, db, proposals, security
from ..render import templates

log = logging.getLogger("eos.routes.docs")
router = APIRouter()


@router.get("/p/{slug}", response_class=HTMLResponse)
async def view_proposal(request: Request, slug: str):
    d = proposals.get_proposal_by_slug(slug)
    listing = db.one("SELECT title, address_line1, city FROM listings WHERE id=?", (d["listing_id"],))
    client = None
    if listing:
        row = db.one(
            """SELECT c.name, c.company FROM listings l
               JOIN clients c ON c.id=l.client_id WHERE l.id=?""",
            (d["listing_id"],),
        )
        client = row
    if d["status"] == "sent" and not d["viewed_at"]:
        proposals.mark_viewed(d["id"])
        log.info("proposal %s viewed from %s", d["id"], security.client_ip(request))
    return templates.TemplateResponse(
        request, "public/proposal.html",
        {
            "d": d,
            "listing": listing,
            "client": client,
            "items": json.loads(d["line_items"] or "[]"),
        },
    )


@router.post("/p/{slug}/accept")
async def accept_proposal(slug: str):
    proposals.accept_by_slug(slug)
    return RedirectResponse(f"/p/{slug}", status_code=303)


@router.post("/p/{slug}/decline")
async def decline_proposal(slug: str):
    proposals.decline_by_slug(slug)
    return RedirectResponse(f"/p/{slug}", status_code=303)


@router.get("/c/{slug}", response_class=HTMLResponse)
async def view_contract(request: Request, slug: str):
    d = contracts.get_contract_by_slug(slug)
    listing = db.one("SELECT title FROM listings WHERE id=?", (d["listing_id"],))
    if d["status"] == "sent":
        contracts.mark_viewed(d["id"])
        log.info("contract %s viewed from %s", d["id"], security.client_ip(request))
    return templates.TemplateResponse(
        request, "public/contract.html", {"d": d, "listing": listing},
    )


@router.post("/c/{slug}/sign")
async def sign_contract(request: Request, slug: str, signer_name: str = Form(...)):
    contracts.sign_by_slug(slug, signer_name, security.client_ip(request))
    return RedirectResponse(f"/c/{slug}", status_code=303)