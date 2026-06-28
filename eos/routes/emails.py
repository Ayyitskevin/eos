import logging

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse

from .. import config, db, emails, mailer, security
from ..vocab import STUDIO_ID

log = logging.getLogger("eos.routes.emails")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])

DOC_TABLES = {
    "proposals": ("proposal", "listing_id"),
    "contracts": ("contract", "listing_id"),
    "invoices": ("invoice", "listing_id"),
    "galleries": ("gallery", "listing_id"),
}


@router.post("/email/{kind}/{doc_id}")
async def send_email(
    kind: str,
    doc_id: int,
    to: str = Form(...),
    subject: str = Form(...),
    message: str = Form(...),
    redirect: str = Form(""),
):
    if kind not in DOC_TABLES:
        raise HTTPException(status_code=404)
    doc_kind, listing_col = DOC_TABLES[kind]
    d = db.one(f"SELECT * FROM {kind} WHERE id=?", (doc_id,))
    if not d:
        raise HTTPException(status_code=404)
    if kind != "galleries" and d["status"] == "draft":
        raise HTTPException(status_code=400, detail="mark the document sent first")
    if not mailer.configured():
        raise HTTPException(status_code=503, detail="email is not configured")
    to, subject = to.strip(), subject.strip()
    if not to or not subject:
        raise HTTPException(status_code=400, detail="to and subject required")
    try:
        mailer.send(to, subject, message)
    except Exception:
        log.exception("send failed for %s %s", kind, doc_id)
        raise HTTPException(status_code=502, detail="SMTP send failed — check logs")
    listing_id = d[listing_col] if d[listing_col] else None
    db.run(
        """INSERT INTO emails_log (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
           VALUES (?,?,?,?,?,?)""",
        (STUDIO_ID, listing_id, doc_kind, doc_id, to, subject),
    )
    log.info("emailed %s %s to %s", doc_kind, doc_id, to)
    return RedirectResponse(redirect or "/admin", status_code=303)


@router.post("/galleries/{gallery_id}/email")
async def email_gallery(
    gallery_id: int,
    to: str = Form(...),
    subject: str = Form(...),
    message: str = Form(...),
    template: str = Form("gallery_delivery"),
):
    g = db.one("SELECT * FROM galleries WHERE id=?", (gallery_id,))
    if not g:
        raise HTTPException(status_code=404)
    if not g["published"]:
        raise HTTPException(status_code=400, detail="publish the gallery first")
    if not mailer.configured():
        raise HTTPException(status_code=503, detail="email is not configured")
    try:
        mailer.send(to.strip(), subject.strip(), message)
    except Exception:
        log.exception("gallery email failed %s", gallery_id)
        raise HTTPException(status_code=502, detail="SMTP send failed")
    db.run(
        """INSERT INTO emails_log (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
           VALUES (?,?,?,?,?,?)""",
        (STUDIO_ID, g["listing_id"], "gallery", gallery_id, to.strip(), subject.strip()),
    )
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)