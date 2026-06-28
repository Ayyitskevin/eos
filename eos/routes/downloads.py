"""Gallery downloads — originals ZIP and single-asset (PIN cookie gate)."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import config, db, jobs, paywall, security
from ..galleries import get_gallery_by_slug
from ..jobs import zip_path
from ..render import templates

log = logging.getLogger("eos.routes.downloads")
router = APIRouter(prefix="/g")


def _gate(request: Request, slug: str, *, require_paid: bool = True):
    g = get_gallery_by_slug(slug)
    if not g["published"]:
        raise HTTPException(status_code=404)
    if not security.gallery_unlocked(request, g["id"]):
        raise HTTPException(status_code=403, detail="gallery access required")
    if require_paid and paywall.payment_required(g["listing_id"]):
        slug_inv = paywall.unpaid_invoice_slug(g["listing_id"])
        raise HTTPException(
            status_code=402,
            detail=f"payment required — pay invoice at /i/{slug_inv}" if slug_inv else "payment required",
        )
    return g


@router.get("/{slug}/download")
async def download_landing(request: Request, slug: str):
    g = _gate(request, slug)
    z = zip_path(g["id"], g["content_rev"])
    if not z.is_file():
        jobs.enqueue("zip_build", {"gallery_id": g["id"], "rev": g["content_rev"]})
        return templates.TemplateResponse(
            request, "public/zip_wait.html", {"g": g},
        )
    return RedirectResponse(f"/g/{slug}/download/zip", status_code=303)


@router.get("/{slug}/download/zip")
async def download_zip(request: Request, slug: str):
    g = _gate(request, slug)
    z = zip_path(g["id"], g["content_rev"])
    if not z.is_file():
        raise HTTPException(status_code=404, detail="zip not ready")
    return FileResponse(z, filename=f"{g['title']}.zip", media_type="application/zip")


@router.get("/{slug}/download/asset/{asset_id}")
async def download_asset(request: Request, slug: str, asset_id: int):
    g = _gate(request, slug)
    a = db.one(
        "SELECT * FROM assets WHERE id=? AND gallery_id=? AND status='ready'",
        (asset_id, g["id"]),
    )
    if not a:
        raise HTTPException(status_code=404)
    path = config.MEDIA_DIR / str(g["id"]) / "original" / a["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=a["filename"], media_type="application/octet-stream")