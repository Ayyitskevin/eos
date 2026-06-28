from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import clients, config, contracts, galleries, invoices, listing_media, listings, proposals, questionnaires, security
from ..render import templates
from ..vocab import LISTING_STATUSES, PROPERTY_TYPES

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/listings/new", response_class=HTMLResponse)
async def listing_new_form(request: Request):
    return templates.TemplateResponse(
        request, "admin/listing_new.html",
        {
            "client_list": clients.list_clients(),
            "property_types": PROPERTY_TYPES,
        },
    )


@router.post("/listings")
async def listing_create(
    title: str = Form(...),
    client_id: str = Form(""),
    property_type: str = Form("residential"),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    zip_code: str = Form(""),
    mls_id: str = Form(""),
    beds: str = Form(""),
    baths: str = Form(""),
    sqft: str = Form(""),
    shoot_date: str = Form(""),
    due_at: str = Form(""),
    access_notes: str = Form(""),
    notes: str = Form(""),
):
    cid = int(client_id) if client_id.strip().isdigit() else None
    lid = listings.create_listing(
        title,
        client_id=cid,
        property_type=property_type,
        address_line1=address_line1,
        address_line2=address_line2,
        city=city,
        state=state,
        zip_code=zip_code,
        mls_id=mls_id,
        beds=float(beds) if beds.strip() else None,
        baths=float(baths) if baths.strip() else None,
        sqft=int(sqft) if sqft.strip().isdigit() else None,
        shoot_date=shoot_date.strip() or None,
        due_at=due_at.strip() or None,
        access_notes=access_notes,
        notes=notes,
    )
    return RedirectResponse(f"/admin/listings/{lid}", status_code=303)


@router.get("/listings/{listing_id}", response_class=HTMLResponse)
async def listing_detail(request: Request, listing_id: int):
    row = listings.get_listing(listing_id)
    client = None
    if row["client_id"]:
        client = clients.get_client(row["client_id"])
    return templates.TemplateResponse(
        request, "admin/listing.html",
        {
            "l": row,
            "address": listings.format_address(row),
            "client": client,
            "shots": listings.listing_shots(listing_id),
            "tasks": listings.listing_tasks(listing_id),
            "galleries": listings.listing_galleries(listing_id),
            "client_list": clients.list_clients(),
            "statuses": LISTING_STATUSES,
            "property_types": PROPERTY_TYPES,
            "invoices": invoices.list_for_listing(listing_id),
            "proposals": proposals.list_for_listing(listing_id),
            "contracts": contracts.list_for_listing(listing_id),
            "proposal_presets": proposals.package_presets(),
            "questionnaires": questionnaires.list_for_listing(listing_id),
            "base_url": config.BASE_URL,
            "media_embeds": listing_media.list_for_listing(listing_id),
            "media_kinds": listing_media.KINDS,
        },
    )


@router.post("/listings/{listing_id}")
async def listing_update(
    listing_id: int,
    title: str = Form(...),
    status: str = Form("lead"),
    client_id: str = Form(""),
    property_type: str = Form("residential"),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    zip_code: str = Form(""),
    mls_id: str = Form(""),
    beds: str = Form(""),
    baths: str = Form(""),
    sqft: str = Form(""),
    shoot_date: str = Form(""),
    due_at: str = Form(""),
    access_notes: str = Form(""),
    notes: str = Form(""),
):
    cid = int(client_id) if client_id.strip().isdigit() else None
    listings.update_listing(
        listing_id,
        title=title, status=status, client_id=cid, property_type=property_type,
        address_line1=address_line1, address_line2=address_line2,
        city=city, state=state, zip=zip_code, mls_id=mls_id,
        beds=float(beds) if beds.strip() else None,
        baths=float(baths) if baths.strip() else None,
        sqft=int(sqft) if sqft.strip().isdigit() else None,
        shoot_date=shoot_date.strip() or None,
        due_at=due_at.strip() or None,
        access_notes=access_notes, notes=notes,
    )
    return RedirectResponse(f"/admin/listings/{listing_id}", status_code=303)


@router.post("/listings/{listing_id}/shots/{shot_id}/toggle")
async def shot_toggle(listing_id: int, shot_id: int, done: bool = Form(False)):
    listings.toggle_shot(shot_id, done)
    return RedirectResponse(f"/admin/listings/{listing_id}#shots", status_code=303)


@router.post("/listings/{listing_id}/tasks/{task_id}/toggle")
async def task_toggle(listing_id: int, task_id: int, done: bool = Form(False)):
    listings.toggle_task(task_id, done)
    return RedirectResponse(f"/admin/listings/{listing_id}#tasks", status_code=303)


@router.post("/listings/{listing_id}/media")
async def listing_add_media(
    listing_id: int,
    kind: str = Form("url"),
    label: str = Form(""),
    embed_url: str = Form(...),
):
    listings.get_listing(listing_id)
    listing_media.add_embed(listing_id, kind=kind, label=label, embed_url=embed_url)
    return RedirectResponse(f"/admin/listings/{listing_id}#media", status_code=303)


@router.post("/listings/{listing_id}/media/{embed_id}/delete")
async def listing_delete_media(listing_id: int, embed_id: int):
    listing_media.delete_embed(embed_id, listing_id)
    return RedirectResponse(f"/admin/listings/{listing_id}#media", status_code=303)


@router.post("/listings/{listing_id}/gallery")
async def listing_create_gallery(listing_id: int):
    row = listings.get_listing(listing_id)
    client_name = None
    if row["client_id"]:
        c = clients.get_client(row["client_id"])
        client_name = c["name"]
    gid = galleries.create_gallery(
        listings.format_address(row) or row["title"],
        listing_id=listing_id,
        client_name=client_name,
    )
    return RedirectResponse(f"/admin/galleries/{gid}", status_code=303)