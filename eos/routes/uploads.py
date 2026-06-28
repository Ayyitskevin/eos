import logging
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from .. import config, db, galleries, jobs, security
from ..imaging import PHOTO_EXTS, VIDEO_EXTS

log = logging.getLogger("eos.routes.uploads")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


def _free_gb() -> float:
    return shutil.disk_usage(config.DATA_DIR).free / 1e9


@router.post("/galleries/{gallery_id}/upload")
async def upload(gallery_id: int, files: list[UploadFile], section_id: int | None = None):
    galleries.get_gallery(gallery_id)
    if section_id is None:
        first = db.one(
            "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1",
            (gallery_id,),
        )
        if first:
            section_id = first["id"]
    if _free_gb() < config.MIN_FREE_GB:
        raise HTTPException(status_code=507, detail="low disk space — upload refused")

    from .. import media_paths

    for sub in ("original", "web", "thumb"):
        media_paths.gallery_subdir(gallery_id, sub)

    accepted, rejected = [], []
    for f in files:
        name = _SAFE_NAME.sub("_", Path(f.filename or "upload").name)
        ext = Path(name).suffix.lower()
        is_video = ext in VIDEO_EXTS
        if ext not in PHOTO_EXTS and not is_video:
            rejected.append(name)
            continue
        stored = f"{uuid.uuid4().hex}{ext}"
        dest = media_paths.gallery_subdir(gallery_id, "original") / stored
        size = 0
        with dest.open("wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
                size += len(chunk)
        kind = "video" if is_video else "photo"
        asset_id = db.run(
            """INSERT INTO assets (gallery_id, section_id, kind, filename, stored, bytes, status)
               VALUES (?,?,?,?,?,?,?)""",
            (gallery_id, section_id, kind, name, stored, size, "pending"),
        )
        jobs.enqueue("video_ready" if is_video else "image_derivatives", {"asset_id": asset_id})
        accepted.append(asset_id)

    if accepted:
        db.run("UPDATE galleries SET content_rev=content_rev+1 WHERE id=?", (gallery_id,))
    log.info("gallery %s: %d accepted, %d rejected", gallery_id, len(accepted), len(rejected))
    return {"accepted": len(accepted), "rejected": rejected}
