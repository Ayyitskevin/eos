"""Auto-send gallery delivery email on publish when configured."""

import logging

from . import db, emails, mailer, studio, tenant

log = logging.getLogger("eos.delivery_notify")


def maybe_send_gallery_email(gallery_id: int) -> bool:
    owner = db.one("SELECT studio_id FROM galleries WHERE id=?", (gallery_id,))
    if not owner:
        return False
    previous_studio = tenant.get_studio_id()
    studio_id = owner["studio_id"]
    tenant.set_studio(studio_id)
    try:
        return _send_gallery_email_for_current_studio(gallery_id, studio_id)
    finally:
        tenant.set_studio(previous_studio)


def _send_gallery_email_for_current_studio(gallery_id: int, studio_id: str) -> bool:
    profile = studio.get_profile()
    if not profile["auto_deliver_email"] or not mailer.configured():
        return False
    g = db.one("SELECT * FROM galleries WHERE id=? AND studio_id=?", (gallery_id, studio_id))
    if not g or not g["published"] or not g["listing_id"]:
        return False
    sent = db.one(
        """SELECT 1 AS x FROM emails_log
           WHERE studio_id=? AND doc_kind='gallery' AND doc_id=? LIMIT 1""",
        (studio_id, gallery_id),
    )
    if sent:
        return False
    listing = db.one(
        """SELECT l.title, c.email, c.name FROM listings l
           LEFT JOIN clients c ON c.id=l.client_id AND c.studio_id=l.studio_id
           WHERE l.id=? AND l.studio_id=?""",
        (g["listing_id"], studio_id),
    )
    if not listing or not listing["email"]:
        return False
    link = f"{tenant.get_base_url()}/g/{g['slug']}"
    subject, body = emails.gallery_delivery(
        client_name=listing["name"] or "there",
        title=g["title"],
        link=link,
        pin=g["pin"],
        expires=g["expires_at"],
    )
    try:
        mailer.send_for_studio(listing["email"], subject, body)
    except Exception:
        log.exception("auto gallery email failed for gallery %s", gallery_id)
        return False
    db.run(
        """INSERT INTO emails_log (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
           VALUES (?,?,?,?,?,?)""",
        (studio_id, g["listing_id"], "gallery", gallery_id, listing["email"], subject),
    )
    log.info("auto-delivered gallery %s to %s", gallery_id, listing["email"])
    return True
