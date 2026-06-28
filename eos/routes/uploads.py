import logging
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from .. import config, db, jobs, security
from ..imaging import PHOTO_EXTS

log = logging.getLogger("eos.routes.uploads")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


def _free_gb() -> float:
    return shutil.disk_usage(config.DATA_DIR).free / 1e9


@router.post("/galleries/{gallery_id}/upload")
async def upload(gallery_id: int, files: list[UploadFile], section_id: int | None = None):
    g = db.one("SELECT id FROM galleries WHERE id=?", (gallery_id,))
    if not g:
        raise HTTPException(status_code=404)
    if section_id is None:
        first = db.one(
            "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1",
            (gallery_id,),
        )
        if first:
            section_id = first["id"]
    if _free_gb() < config.MIN_FREE_GB:
        raise HTTPException(status_code=507, detail="low disk space — upload refused")

    base = config.MEDIA_DIR / str(gallery_id)
    for sub in ("original", "web", "thumb"):
        (base / sub).mkdir(parents=True, exist_ok=True)

    accepted, rejected = [], []
    for f in files:
        name = _SAFE_NAME.sub("_", Path(f.filename or "upload").name)
        ext = Path(name).suffix.lower()
        if ext not in PHOTO_EXTS:
            rejected.append(name)
            continue
        stored = f"{uuid.uuid4().hex}{ext}"
        dest = base / "original" / stored
        size = 0
        with dest.open("wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
                size += len(chunk)
        asset_id = db.run(
            """INSERT INTO assets (gallery_id, section_id, kind, filename, stored, bytes)
               VALUES (?,?,?,?,?,?)""",
            (gallery_id, section_id, "photo", name, stored, size),
        )
        jobs.enqueue("image_derivatives", {"asset_id": asset_id})
        accepted.append(asset_id)

    if accepted:
        db.run("UPDATE galleries SET content_rev=content_rev+1 WHERE id=?", (gallery_id,))
    log.info("gallery %s: %d accepted, %d rejected", gallery_id, len(accepted), len(rejected))
    return {"accepted": len(accepted), "rejected": rejected}