"""Serve gallery derivatives — admin (open) and public (PIN cookie)."""

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .. import config, db, imaging, paywall, security
from ..galleries import get_gallery_by_slug

router = APIRouter()
VARIANTS = {"thumb", "web", "original", "export"}


def _asset_path(gallery_id: int, asset, variant: str) -> Path:
    base = config.MEDIA_DIR / str(gallery_id)
    stem = Path(asset["stored"]).stem
    if variant == "original":
        return base / "original" / asset["stored"]
    if variant == "thumb":
        return base / "thumb" / f"{stem}.jpg"
    if variant == "web":
        return base / "web" / f"{stem}.jpg"
    raise HTTPException(status_code=404)


@router.get("/admin/galleries/{gallery_id}/media/{variant}/{asset_id}")
async def admin_media(gallery_id: int, variant: str, asset_id: int):
    if variant not in VARIANTS - {"export"}:
        raise HTTPException(status_code=404)
    a = db.one(
        "SELECT * FROM assets WHERE id=? AND gallery_id=? AND status='ready'",
        (asset_id, gallery_id),
    )
    if not a:
        raise HTTPException(status_code=404)
    path = _asset_path(gallery_id, a, variant)
    if not path.is_file():
        raise HTTPException(status_code=404)
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@router.get("/media/{slug}/{variant}/{asset_id}")
async def public_media(request: Request, slug: str, variant: str, asset_id: int):
    if variant not in ("thumb", "web"):
        raise HTTPException(status_code=404)
    g = get_gallery_by_slug(slug)
    if not g["published"]:
        raise HTTPException(status_code=404)
    if not security.gallery_unlocked(request, g["id"]):
        raise HTTPException(status_code=403)
    a = db.one(
        "SELECT * FROM assets WHERE id=? AND gallery_id=? AND status='ready'",
        (asset_id, g["id"]),
    )
    if not a:
        raise HTTPException(status_code=404)
    path = _asset_path(g["id"], a, variant)
    if not path.is_file():
        raise HTTPException(status_code=404)
    if paywall.payment_required(g["listing_id"]) and paywall.watermark_previews():
        data = imaging.apply_preview_watermark(path)
        return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})