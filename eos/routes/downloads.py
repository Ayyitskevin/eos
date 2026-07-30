"""Gallery downloads — originals ZIP and single-asset (PIN cookie gate)."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from .. import db, galleries, jobs, paywall, video_render
from ..jobs import zip_path
from ..render import templates

log = logging.getLogger("eos.routes.downloads")
router = APIRouter(prefix="/g")


def _gate(request: Request, slug: str, *, require_paid: bool = True):
    g = galleries.get_gallery_by_slug(slug)
    galleries.require_public_access(request, g)
    if require_paid and paywall.payment_required(g["listing_id"]):
        slug_inv = paywall.unpaid_invoice_slug(g["listing_id"])
        raise HTTPException(
            status_code=402,
            detail=f"payment required — pay invoice at /i/{slug_inv}"
            if slug_inv
            else "payment required",
        )
    return g


@router.get("/{slug}/download")
async def download_landing(request: Request, slug: str):
    g = _gate(request, slug)
    z = zip_path(g["id"], g["content_rev"])
    if not z.is_file():
        jobs.enqueue("zip_build", {"gallery_id": g["id"], "rev": g["content_rev"]})
        return templates.TemplateResponse(
            request,
            "public/zip_wait.html",
            {"g": g},
        )
    return RedirectResponse(f"/g/{slug}/download/zip", status_code=303)


@router.get("/{slug}/download/zip")
async def download_zip(request: Request, slug: str):
    g = _gate(request, slug)
    z = zip_path(g["id"], g["content_rev"])
    if not z.is_file():
        raise HTTPException(status_code=404, detail="zip not ready")
    return FileResponse(z, filename=f"{g['title']}.zip", media_type="application/zip")


@router.get("/{slug}/video/{fmt}")
async def download_video(request: Request, slug: str, fmt: str):
    g = _gate(request, slug)
    path = video_render.ready_file(g["id"], fmt)
    if not path:
        raise HTTPException(status_code=404, detail="video not ready")
    return FileResponse(path, filename=f"{g['title']}-{fmt}.mp4", media_type="video/mp4")


@router.get("/{slug}/download/asset/{asset_id}")
async def download_asset(request: Request, slug: str, asset_id: int):
    g = _gate(request, slug)
    a = db.one(
        "SELECT * FROM assets WHERE id=? AND gallery_id=? AND status='ready'",
        (asset_id, g["id"]),
    )
    if not a:
        raise HTTPException(status_code=404)
    from .. import media_paths

    path = media_paths.gallery_dir(g["id"]) / "original" / a["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=a["filename"], media_type="application/octet-stream")
