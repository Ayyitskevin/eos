"""Lightweight listing status hooks — solo-operator guardrails."""

import logging

from . import db
from .vocab import STUDIO_ID

log = logging.getLogger("eos.automations")


def _trigger(event: str, listing_id: int, *, event_key: str | None = None) -> None:
    try:
        from . import sequences

        sequences.trigger(event, listing_id, event_key=event_key)
    except Exception:
        log.exception("sequence trigger %s failed for listing %s", event, listing_id)
        raise


def on_questionnaire_completed(listing_id: int) -> None:
    row = db.one("SELECT status FROM listings WHERE id=? AND studio_id=?", (listing_id, STUDIO_ID))
    if row and row["status"] == "booked":
        db.run(
            """UPDATE listings SET status='shooting', updated_at=datetime('now')
               WHERE id=? AND studio_id=?""",
            (listing_id, STUDIO_ID),
        )
        log.info("listing %s auto-advanced booked → shooting (questionnaire)", listing_id)


def on_listing_booked(listing_id: int) -> None:
    event_key = f"listing:{listing_id}:booked"
    _trigger("listing.booked", listing_id, event_key=event_key)
    _webhook(
        "booking.created",
        listing_id,
        event_key=event_key,
        extra={"status": "booked"},
    )


def on_listing_delivered(
    listing_id: int,
    *,
    gallery_id: int | None = None,
    delivery_round: int | None = None,
) -> None:
    if delivery_round is None:
        row = db.one(
            "SELECT revision_round FROM listings WHERE id=? AND studio_id=?",
            (listing_id, STUDIO_ID),
        )
        delivery_round = int(row["revision_round"] or 0) if row else 0
    event_key = f"listing:{listing_id}:delivered:r{delivery_round}"
    extra = {"status": "delivered", "delivery_round": delivery_round}
    if gallery_id is not None:
        extra["gallery_id"] = gallery_id
    _trigger("listing.delivered", listing_id, event_key=event_key)
    _webhook("listing.delivered", listing_id, event_key=event_key, extra=extra)


def on_gallery_published(listing_id: int | None, n_assets: int) -> None:
    if not listing_id or n_assets == 0:
        return
    on_listing_delivered(listing_id)


def on_gallery_published_email(gallery_id: int) -> None:
    try:
        from . import delivery_notify

        delivery_notify.enqueue_gallery_email(gallery_id)
    except Exception:
        log.exception("auto gallery email failed for %s", gallery_id)
        raise


def on_proposal_sent(listing_id: int) -> None:
    event_key = f"listing:{listing_id}:proposal-sent"
    _trigger("proposal.sent", listing_id, event_key=event_key)


def on_invoice_paid(listing_id: int | None, *, invoice_id: int | None = None) -> None:
    if not listing_id:
        return
    db.run(
        """UPDATE listing_tasks SET done=1
           WHERE listing_id=? AND studio_id=? AND label LIKE '%invoice%'""",
        (listing_id, STUDIO_ID),
    )
    event_key = f"invoice:{invoice_id or listing_id}:paid"
    _webhook("invoice.paid", listing_id, event_key=event_key)


def _webhook(event: str, listing_id: int, *, event_key: str | None = None, extra=None) -> None:
    try:
        from . import webhooks

        payload = {"listing_id": listing_id}
        if extra:
            payload.update(extra)
        webhooks.dispatch(event, payload, event_key=event_key)
    except Exception:
        log.exception("webhook %s failed for listing %s", event, listing_id)
        raise


@db.transactional(immediate=True)
def on_deposit_paid(
    inquiry_id: int,
    listing_id: int | None,
    *,
    payment_event_created: float | None = None,
) -> bool:
    with db.tx() as con:
        claimed = con.execute(
            """UPDATE inquiries SET status='confirmed'
               WHERE id=? AND studio_id=? AND status='pending_payment'""",
            (inquiry_id, STUDIO_ID),
        )
    if claimed.rowcount != 1:
        return False
    referral = db.one(
        "SELECT referral_id FROM inquiries WHERE id=? AND studio_id=?",
        (inquiry_id, STUDIO_ID),
    )
    if referral and referral["referral_id"]:
        from . import referrals

        referrals.finalize_inquiry(inquiry_id, payment_event_created=payment_event_created)
    if not listing_id:
        return True
    inq = db.one(
        "SELECT appointment_id FROM inquiries WHERE id=? AND studio_id=?",
        (inquiry_id, STUDIO_ID),
    )
    if inq and inq["appointment_id"]:
        db.run(
            "UPDATE appointments SET status='confirmed' WHERE id=? AND studio_id=?",
            (inq["appointment_id"], STUDIO_ID),
        )
    db.run(
        """UPDATE listings SET status='booked', updated_at=datetime('now')
           WHERE id=? AND studio_id=?""",
        (listing_id, STUDIO_ID),
    )
    prop = db.one(
        """SELECT id FROM proposals
           WHERE listing_id=? AND studio_id=? AND status='draft'
           ORDER BY id DESC LIMIT 1""",
        (listing_id, STUDIO_ID),
    )
    if prop:
        from . import proposals

        proposals.mark_sent(prop["id"])
    else:
        on_listing_booked(listing_id)
    return True
