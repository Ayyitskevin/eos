import logging
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

from .. import config, db, security
from ..vocab import STUDIO_ID

log = logging.getLogger("eos.routes.brand_kits")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])

_SAFE = re.compile(r"[^A-Za-z0-9._ -]")
_LOGO_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".svg"}


@router.post("/clients/{client_id}/brand-kit")
async def upload_brand_kit(
    client_id: int,
    file: UploadFile,
    label: str = Form("Logo"),
    position: str = Form("br"),
    opacity: int = Form(100),
    scale_pct: int = Form(18),
    margin_pct: int = Form(2),
):
    if not db.one("SELECT 1 AS x FROM clients WHERE id=? AND studio_id=?", (client_id, STUDIO_ID)):
        raise HTTPException(status_code=404)
    name = _SAFE.sub("_", Path(file.filename or "logo.png").name)
    ext = Path(name).suffix.lower()
    if ext not in _LOGO_EXTS:
        raise HTTPException(status_code=400, detail="unsupported logo format")
    stored = f"{uuid.uuid4().hex}{ext}"
    dest_dir = config.BRAND_DIR / str(client_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / stored
    with dest.open("wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)
    db.run("UPDATE brand_kits SET active=0 WHERE client_id=?", (client_id,))
    db.run(
        """INSERT INTO brand_kits
           (studio_id, client_id, label, stored, position, opacity, scale_pct, margin_pct)
           VALUES (?,?,?,?,?,?,?,?)""",
        (STUDIO_ID, client_id, label.strip(), stored, position, opacity, scale_pct, margin_pct),
    )
    db.audit("admin", "brand_kit.upload", f"client_id={client_id}")
    log.info("brand kit uploaded for client %s", client_id)
    return RedirectResponse(f"/admin/clients/{client_id}", status_code=303)
