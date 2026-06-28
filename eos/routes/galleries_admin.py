from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, galleries, jobs, listings, security, studio
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
    by_section: dict[int, list] = {s["id"]: [] for s in sections}
    unsectioned = []
    for a in assets:
        if a["section_id"] and a["section_id"] in by_section:
            by_section[a["section_id"]].append(a)
        else:
            unsectioned.append(a)
    n_pending = sum(1 for a in assets if a["status"] == "pending")
    return templates.TemplateResponse(
        request, "admin/gallery.html",
        {
            "g": g,
            "sections": sections,
            "assets": assets,
            "by_section": by_section,
            "unsectioned": unsectioned,
            "listing": listing,
            "listings": listings.list_listings(),
            "presets": studio.list_crop_presets(),
            "n_pending": n_pending,
            "base_url": config.BASE_URL,
        },
    )


@router.post("/galleries/{gallery_id}/exports")
async def build_exports(gallery_id: int):
    galleries.get_gallery(gallery_id)
    jobs.enqueue("gallery_exports", {"gallery_id": gallery_id})
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


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