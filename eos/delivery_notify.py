"""Durable gallery-delivery email intents and recovery."""

import logging

from . import db, emails, mailer, portal, security, studio, tenant
from .vocab import STUDIO_ID

log = logging.getLogger("eos.delivery_notify")
_CLAIM_TIMEOUT = "-15 minutes"
_UNKNOWN_OUTCOME = "Delivery outcome unknown; verify the provider before retrying."
_NOT_DELIVERED = "Operator confirmed the gallery delivery email was not delivered."


class DeliveryNotificationNotFound(LookupError):
    """The requested notification does not belong to the active studio."""


class DeliveryNotificationReconciliationConflict(RuntimeError):
    """The notification cannot be safely reconciled in its current state."""


def _is_unknown_outcome(error: str | None) -> bool:
    normalized = (error or "").strip().lower()
    return normalized.startswith(_UNKNOWN_OUTCOME.lower()) or normalized.startswith(
        "email outcome unknown after "
    )


def list_notifications(limit: int = 30):
    return db.all_(
        """SELECT n.*, g.title AS gallery_title, l.title AS listing_title, c.email AS to_email,
                  CASE WHEN n.status='failed' AND n.claimed_at IS NULL
                         AND n.attempt_token IS NULL
                         AND (lower(COALESCE(n.error,'')) LIKE 'delivery outcome unknown;%'
                              OR lower(COALESCE(n.error,'')) LIKE
                                 'email outcome unknown after %')
                       THEN 1 ELSE 0 END AS reconcile_ready
           FROM delivery_notifications n
           JOIN galleries g ON g.id=n.gallery_id AND g.studio_id=n.studio_id
           LEFT JOIN listings l ON l.id=g.listing_id AND l.studio_id=g.studio_id
           LEFT JOIN clients c ON c.id=l.client_id AND c.studio_id=l.studio_id
           WHERE n.studio_id=?
           ORDER BY n.created_at DESC, n.id DESC LIMIT ?""",
        (STUDIO_ID, limit),
    )


def enqueue_gallery_email(gallery_id: int, *, event_key: str | None = None) -> bool:
    """Persist one auto-delivery intent per immutable delivery event."""
    owner = db.one("SELECT studio_id FROM galleries WHERE id=?", (gallery_id,))
    if not owner:
        return False
    previous_studio = tenant.get_studio_id()
    studio_id = owner["studio_id"]
    tenant.set_studio(studio_id)
    try:
        profile = studio.get_profile()
        if not profile["auto_deliver_email"]:
            return False
        gallery = db.one(
            """SELECT g.listing_id, g.published, l.revision_round
               FROM galleries g
               LEFT JOIN listings l ON l.id=g.listing_id AND l.studio_id=g.studio_id
               WHERE g.id=? AND g.studio_id=?""",
            (gallery_id, studio_id),
        )
        if not gallery or not gallery["published"] or not gallery["listing_id"]:
            return False
        stable_event_key = event_key or (
            f"listing:{gallery['listing_id']}:delivered:r{int(gallery['revision_round'] or 0)}"
        )
        if not stable_event_key or len(stable_event_key) > 200:
            raise ValueError("invalid delivery notification event key")
        with db.tx() as con:
            cur = con.execute(
                """INSERT OR IGNORE INTO delivery_notifications
                   (studio_id, gallery_id, event_key, status, updated_at)
                   VALUES (?,?,?,'pending',datetime('now'))""",
                (studio_id, gallery_id, stable_event_key),
            )
            if cur.rowcount:
                return True
            existing = con.execute(
                """SELECT status FROM delivery_notifications
                   WHERE studio_id=? AND event_key=?""",
                (studio_id, stable_event_key),
            ).fetchone()
            return bool(existing and existing["status"] == "pending")
    finally:
        tenant.set_studio(previous_studio)


def _fail_stale_claims() -> int:
    """Surface interrupted sends for operator review instead of auto-resending."""
    with db.tx(immediate=True) as con:
        cur = con.execute(
            f"""UPDATE delivery_notifications
                SET status='failed', error=?, claimed_at=NULL, attempt_token=NULL,
                    updated_at=datetime('now')
                WHERE status='pending'
                  AND claimed_at < datetime('now','{_CLAIM_TIMEOUT}')""",
            (_UNKNOWN_OUTCOME,),
        )
        return cur.rowcount


def _claim_notification(notification_id: int, studio_id: str):
    attempt_token = security.new_token()
    con = db.connect()
    try:
        cur = con.execute(
            """UPDATE delivery_notifications
                SET claimed_at=datetime('now'), attempts=attempts+1,
                    attempt_token=?, updated_at=datetime('now')
                WHERE id=? AND studio_id=? AND status='pending'
                  AND claimed_at IS NULL""",
            (attempt_token, notification_id, studio_id),
        )
        if cur.rowcount != 1:
            con.commit()
            return None
        row = con.execute(
            "SELECT * FROM delivery_notifications WHERE id=? AND studio_id=?",
            (notification_id, studio_id),
        ).fetchone()
        con.commit()
        return row
    finally:
        con.close()


def process_notification(notification_id: int, *, studio_id: str | None = None) -> bool:
    """Claim and send one delivery notification; leave config-off work pending."""
    if not mailer.configured():
        return False
    claim_studio = studio_id or tenant.get_studio_id()
    notification = _claim_notification(notification_id, claim_studio)
    if not notification:
        return False
    previous_studio = tenant.get_studio_id()
    studio_id = notification["studio_id"]
    gallery_id = notification["gallery_id"]
    attempt_token = notification["attempt_token"]
    tenant.set_studio(studio_id)
    provider_accepted = False
    try:
        gallery = db.one(
            "SELECT * FROM galleries WHERE id=? AND studio_id=?",
            (gallery_id, studio_id),
        )
        if not gallery or not gallery["published"] or not gallery["listing_id"]:
            raise RuntimeError("gallery is not publishable")
        listing = db.one(
            """SELECT l.id, l.title, l.client_id, c.email, c.name, c.portal_token
               FROM listings l
               LEFT JOIN clients c ON c.id=l.client_id AND c.studio_id=l.studio_id
               WHERE l.id=? AND l.studio_id=?""",
            (gallery["listing_id"], studio_id),
        )
        if not listing or not listing["client_id"] or not listing["email"]:
            raise RuntimeError("gallery client email is missing")
        already_sent = db.one(
            """SELECT 1 AS x FROM emails_log
               WHERE studio_id=? AND (
                   (doc_kind='gallery_delivery' AND doc_id=?)
                   OR (? LIKE '%:legacy:%' AND doc_kind='gallery' AND doc_id=?)
               ) LIMIT 1""",
            (studio_id, notification_id, notification["event_key"], gallery_id),
        )
        if already_sent:
            with db.tx(immediate=True) as con:
                con.execute(
                    """UPDATE delivery_notifications
                       SET status='sent', error=NULL, claimed_at=NULL, attempt_token=NULL,
                           updated_at=datetime('now')
                       WHERE id=? AND studio_id=? AND status='pending'
                         AND attempt_token=?""",
                    (notification_id, studio_id, attempt_token),
                )
            return False
        repeat = portal.repeat_links(
            listing["client_id"],
            portal_token=listing["portal_token"],
        )
        link = f"{tenant.get_base_url()}/g/{gallery['slug']}"
        subject, body = emails.gallery_delivery(
            client_name=listing["name"] or "there",
            title=gallery["title"],
            link=link,
            pin=gallery["pin"],
            expires=gallery["expires_at"],
            rebook_link=repeat["rebook_url"],
            referral_link=repeat["referral_url"] or "",
        )
        mailer.send_for_studio(listing["email"], subject, body)
        provider_accepted = True
        with db.tx(immediate=True) as con:
            updated = con.execute(
                """UPDATE delivery_notifications
                   SET status='sent', error=NULL, claimed_at=NULL, attempt_token=NULL,
                       updated_at=datetime('now')
                   WHERE id=? AND studio_id=? AND status='pending'
                     AND attempt_token=?""",
                (notification_id, studio_id, attempt_token),
            )
            if updated.rowcount != 1:
                raise RuntimeError("delivery notification claim was lost after provider acceptance")
            con.execute(
                """INSERT INTO emails_log
                   (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
                   VALUES (?,?,?,?,?,?)""",
                (
                    studio_id,
                    gallery["listing_id"],
                    "gallery_delivery",
                    notification_id,
                    listing["email"],
                    subject,
                ),
            )
        log.info("auto-delivered gallery %s to %s", gallery_id, listing["email"])
        return True
    except Exception as exc:
        error = str(exc)[:500]
        if provider_accepted:
            error = f"{_UNKNOWN_OUTCOME} {error}"[:500]
        with db.tx(immediate=True) as con:
            con.execute(
                """UPDATE delivery_notifications
                   SET status='failed', error=?, claimed_at=NULL, attempt_token=NULL,
                       updated_at=datetime('now')
                   WHERE id=? AND studio_id=? AND status='pending'
                     AND attempt_token=?""",
                (error, notification_id, studio_id, attempt_token),
            )
        log.error("gallery delivery notification %s failed: %s", notification_id, exc)
        return False
    finally:
        tenant.set_studio(previous_studio)


def process_pending(limit: int = 20) -> int:
    _fail_stale_claims()
    if not mailer.configured():
        return 0
    pending = db.all_(
        """SELECT id, studio_id FROM delivery_notifications
            WHERE status='pending'
              AND claimed_at IS NULL
            ORDER BY created_at, id LIMIT ?""",
        (limit,),
    )
    return sum(1 for row in pending if process_notification(row["id"], studio_id=row["studio_id"]))


def retry_notification(notification_id: int) -> bool:
    from fastapi import HTTPException

    row = db.one(
        "SELECT id FROM delivery_notifications WHERE id=? AND studio_id=?",
        (notification_id, STUDIO_ID),
    )
    if not row:
        raise HTTPException(status_code=404)
    with db.tx() as con:
        cur = con.execute(
            """UPDATE delivery_notifications
               SET status='pending', error=NULL, claimed_at=NULL, attempt_token=NULL,
                   updated_at=datetime('now')
               WHERE id=? AND studio_id=? AND status='failed'
                 AND claimed_at IS NULL AND attempt_token IS NULL
                 AND NOT (lower(COALESCE(error,'')) LIKE 'delivery outcome unknown;%'
                          OR lower(COALESCE(error,'')) LIKE
                             'email outcome unknown after %')""",
            (notification_id, STUDIO_ID),
        )
        if cur.rowcount:
            db.audit("admin", "delivery_notification.retry", f"notification={notification_id}")
        return cur.rowcount == 1


def reconcile_notification(notification_id: int, *, delivered: bool) -> None:
    """Record a provider-verified outcome without sending another email."""
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        row = con.execute(
            """SELECT n.*, g.listing_id, g.title AS gallery_title,
                      c.email AS to_email
               FROM delivery_notifications n
               JOIN galleries g ON g.id=n.gallery_id AND g.studio_id=n.studio_id
               LEFT JOIN listings l ON l.id=g.listing_id AND l.studio_id=g.studio_id
               LEFT JOIN clients c ON c.id=l.client_id AND c.studio_id=l.studio_id
               WHERE n.id=? AND n.studio_id=?""",
            (notification_id, studio_id),
        ).fetchone()
        if not row:
            raise DeliveryNotificationNotFound("gallery delivery notification not found")
        if row["claimed_at"] is not None or row["attempt_token"] is not None:
            raise DeliveryNotificationReconciliationConflict(
                "gallery delivery notification still has an active claim"
            )
        if row["status"] != "failed" or not _is_unknown_outcome(row["error"]):
            raise DeliveryNotificationReconciliationConflict(
                "only unknown gallery delivery outcomes can be reconciled"
            )

        if delivered:
            cur = con.execute(
                """UPDATE delivery_notifications
                   SET status='sent', error=NULL, claimed_at=NULL, attempt_token=NULL,
                       updated_at=datetime('now')
                   WHERE id=? AND studio_id=? AND status='failed' AND error=?
                     AND claimed_at IS NULL AND attempt_token IS NULL""",
                (notification_id, studio_id, row["error"]),
            )
            if cur.rowcount != 1:
                raise DeliveryNotificationReconciliationConflict(
                    "gallery delivery reconciliation state changed"
                )
            if row["to_email"]:
                con.execute(
                    """INSERT INTO emails_log
                       (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
                       SELECT ?, ?, 'gallery_delivery', ?, ?, ?
                       WHERE NOT EXISTS (
                           SELECT 1 FROM emails_log
                           WHERE studio_id=? AND doc_kind='gallery_delivery' AND doc_id=?
                       )""",
                    (
                        studio_id,
                        row["listing_id"],
                        notification_id,
                        row["to_email"],
                        f"Provider-confirmed delivery — {row['gallery_title']}",
                        studio_id,
                        notification_id,
                    ),
                )
            action = "delivery_notification.reconcile.delivered"
        else:
            cur = con.execute(
                """UPDATE delivery_notifications
                   SET error=?, claimed_at=NULL, attempt_token=NULL,
                       updated_at=datetime('now')
                   WHERE id=? AND studio_id=? AND status='failed' AND error=?
                     AND claimed_at IS NULL AND attempt_token IS NULL""",
                (_NOT_DELIVERED, notification_id, studio_id, row["error"]),
            )
            if cur.rowcount != 1:
                raise DeliveryNotificationReconciliationConflict(
                    "gallery delivery reconciliation state changed"
                )
            action = "delivery_notification.reconcile.not_delivered"
        db.audit("admin", action, f"notification={notification_id}")


def maybe_send_gallery_email(gallery_id: int) -> bool:
    """Compatibility wrapper: persist first, then process only after commit."""
    if not enqueue_gallery_email(gallery_id):
        return False
    owner = db.one("SELECT studio_id FROM galleries WHERE id=?", (gallery_id,))
    if not owner:
        return False
    row = db.one(
        """SELECT id FROM delivery_notifications
           WHERE studio_id=? AND gallery_id=? AND status='pending'
           ORDER BY id DESC LIMIT 1""",
        (owner["studio_id"], gallery_id),
    )
    if not row:
        return False
    if db.in_transaction():
        db.after_commit(
            lambda nid=row["id"], sid=owner["studio_id"]: process_notification(nid, studio_id=sid)
        )
        return True
    return process_notification(row["id"], studio_id=owner["studio_id"])
