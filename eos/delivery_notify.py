"""Auto-send gallery delivery email on publish when configured."""

import logging

from . import config, db, emails, mailer, studio
from .vocab import STUDIO_ID

log = logging.getLogger("eos.delivery_notify")


def maybe_send_gallery_email(gallery_id: int) -> bool:
    profile = studio.get_profile()
    if not profile["auto_deliver_email"] or not mailer.configured():
        return False
    g = db.one("SELECT * FROM galleries WHERE id=? AND studio_id=?", (gallery_id, STUDIO_ID))
    if not g or not g["published"] or not g["listing_id"]:
        return False
    sent = db.one(
        "SELECT 1 AS x FROM emails_log WHERE doc_kind='gallery' AND doc_id=? LIMIT 1",
        (gallery_id,),
    )
    if sent:
        return False
    listing = db.one(
        """SELECT l.title, c.email, c.name FROM listings l
           LEFT JOIN clients c ON c.id=l.client_id WHERE l.id=?""",
        (g["listing_id"],),
    )
    if not listing or not listing["email"]:
        return False
    link = f"{config.BASE_URL}/g/{g['slug']}"
    subject, body = emails.gallery_delivery(
        client_name=listing["name"] or "there",
        title=g["title"],
        link=link,
        pin=g["pin"],
        expires=g["expires_at"],
    )
    try:
        mailer.send(listing["email"], subject, body)
    except Exception:
        log.exception("auto gallery email failed for gallery %s", gallery_id)
        return False
    db.run(
        """INSERT INTO emails_log (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
           VALUES (?,?,?,?,?,?)""",
        (STUDIO_ID, g["listing_id"], "gallery", gallery_id, listing["email"], subject),
    )
    log.info("auto-delivered gallery %s to %s", gallery_id, listing["email"])
    return True