from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, galleries, listings, security
from ..render import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/galleries", response_class=HTMLResponse)
async def galleries_index(request: Request):
    return templates.TemplateResponse(
        request, "admin/galleries.html",
        {"galleries": galleries.list_galleries(), "base_url": config.BASE_URL},
    )


@router.get("/galleries/{gallery_id}", response_class=HTMLResponse)
async def gallery_detail(request: Request, gallery_id: int):
    g = galleries.get_gallery(gallery_id)
    sections = galleries.gallery_sections(gallery_id)
    assets = galleries.gallery_assets(gallery_id)
    listing = None
    if g["listing_id"]:
        listing = listings.get_listing(g["listing_id"])
    return templates.TemplateResponse(
        request, "admin/gallery.html",
        {
            "g": g,
            "sections": sections,
            "assets": assets,
            "listing": listing,
            "listings": listings.list_listings(),
            "base_url": config.BASE_URL,
        },
    )


@router.post("/galleries/{gallery_id}/settings")
async def gallery_settings(
    gallery_id: int,
    title: str = Form(...),
    client_name: str = Form(""),
    pin: str = Form(...),
    expires_at: str = Form(""),
    published: bool = Form(False),
    listing_id: str = Form(""),
):
    lid = int(listing_id) if listing_id.strip().isdigit() else None
    galleries.update_gallery_settings(
        gallery_id,
        title=title,
        client_name=client_name.strip() or None,
        pin=pin,
        expires_at=expires_at.strip() or None,
        published=published,
        listing_id=lid,
    )
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)