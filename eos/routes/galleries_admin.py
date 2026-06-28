from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, db, galleries, jobs, listings, security, studio
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
            "mailer_on": __import__("eos.mailer", fromlist=["configured"]).configured(),
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


def _get_section(gallery_id: int, section_id: int):
    from fastapi import HTTPException
    s = db.one("SELECT * FROM sections WHERE id=? AND gallery_id=?", (section_id, gallery_id))
    if not s:
        raise HTTPException(status_code=404)
    return s


@router.post("/galleries/{gallery_id}/sections")
async def add_section(gallery_id: int, name: str = Form(...)):
    galleries.get_gallery(gallery_id)
    row = db.one(
        "SELECT COALESCE(MAX(position),-1)+1 AS p FROM sections WHERE gallery_id=?",
        (gallery_id,),
    )
    db.run(
        "INSERT INTO sections (gallery_id, name, position) VALUES (?,?,?)",
        (gallery_id, name.strip(), row["p"]),
    )
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/sections/{section_id}/rename")
async def rename_section(gallery_id: int, section_id: int, name: str = Form(...)):
    from fastapi import HTTPException
    _get_section(gallery_id, section_id)
    if not name.strip():
        raise HTTPException(status_code=400, detail="name required")
    db.run("UPDATE sections SET name=? WHERE id=?", (name.strip(), section_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/sections/{section_id}/move")
async def reorder_section(gallery_id: int, section_id: int, dir: str = Form(...)):
    from fastapi import HTTPException
    if dir not in ("up", "down"):
        raise HTTPException(status_code=400, detail="dir must be up or down")
    _get_section(gallery_id, section_id)
    ids = [
        r["id"]
        for r in db.all_(
            "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id",
            (gallery_id,),
        )
    ]
    i = ids.index(section_id)
    j = i - 1 if dir == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for pos, sid in enumerate(ids):
            db.run("UPDATE sections SET position=? WHERE id=?", (pos, sid))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/sections/{section_id}/delete")
async def delete_section(gallery_id: int, section_id: int):
    db.run("DELETE FROM sections WHERE id=? AND gallery_id=?", (section_id, gallery_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/assets/{asset_id}/section")
async def move_asset(gallery_id: int, asset_id: int, section_id: str = Form("")):
    sid = int(section_id) if section_id.strip().isdigit() else None
    db.run(
        "UPDATE assets SET section_id=? WHERE id=? AND gallery_id=?",
        (sid, asset_id, gallery_id),
    )
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/assets/{asset_id}/move")
async def reorder_asset(gallery_id: int, asset_id: int, dir: str = Form(...)):
    from fastapi import HTTPException
    if dir not in ("left", "right"):
        raise HTTPException(status_code=400, detail="dir must be left or right")
    a = db.one("SELECT section_id FROM assets WHERE id=? AND gallery_id=?", (asset_id, gallery_id))
    if not a:
        raise HTTPException(status_code=404)
    siblings = db.all_(
        """SELECT id FROM assets WHERE gallery_id=? AND section_id IS ?
           ORDER BY position, id""",
        (gallery_id, a["section_id"]),
    )
    ids = [s["id"] for s in siblings]
    i = ids.index(asset_id)
    j = i - 1 if dir == "left" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for pos, aid in enumerate(ids):
            db.run("UPDATE assets SET position=? WHERE id=?", (pos, aid))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)