"""Lightweight listing status hooks — solo-operator guardrails."""

import logging

from . import db

log = logging.getLogger("eos.automations")


def _trigger(event: str, listing_id: int) -> None:
    try:
        from . import sequences
        sequences.trigger(event, listing_id)
    except Exception:
        log.exception("sequence trigger %s failed for listing %s", event, listing_id)


def on_questionnaire_completed(listing_id: int) -> None:
    row = db.one("SELECT status FROM listings WHERE id=?", (listing_id,))
    if row and row["status"] == "booked":
        db.run("UPDATE listings SET status='shooting', updated_at=datetime('now') WHERE id=?", (listing_id,))
        log.info("listing %s auto-advanced booked → shooting (questionnaire)", listing_id)


def on_listing_booked(listing_id: int) -> None:
    _trigger("listing.booked", listing_id)


def on_gallery_published(listing_id: int | None, n_assets: int) -> None:
    if not listing_id or n_assets == 0:
        return
    row = db.one("SELECT status FROM listings WHERE id=?", (listing_id,))
    if row and row["status"] in ("shooting", "editing"):
        db.run(
            "UPDATE listings SET status='delivered', updated_at=datetime('now') WHERE id=?",
            (listing_id,),
        )
        log.info("listing %s auto-advanced → delivered (gallery published)", listing_id)
        _trigger("listing.delivered", listing_id)


def on_gallery_published_email(gallery_id: int) -> None:
    try:
        from . import delivery_notify
        delivery_notify.maybe_send_gallery_email(gallery_id)
    except Exception:
        log.exception("auto gallery email failed for %s", gallery_id)


def on_proposal_sent(listing_id: int) -> None:
    _trigger("proposal.sent", listing_id)


def on_invoice_paid(listing_id: int | None) -> None:
    if not listing_id:
        return
    db.run(
        "UPDATE listing_tasks SET done=1 WHERE listing_id=? AND label LIKE '%invoice%'",
        (listing_id,),
    )


def on_deposit_paid(inquiry_id: int, listing_id: int | None) -> None:
    db.run(
        "UPDATE inquiries SET status='confirmed' WHERE id=? AND status='pending_payment'",
        (inquiry_id,),
    )
    if not listing_id:
        return
    inq = db.one("SELECT appointment_id FROM inquiries WHERE id=?", (inquiry_id,))
    if inq and inq.get("appointment_id"):
        db.run(
            "UPDATE appointments SET status='confirmed' WHERE id=?",
            (inq["appointment_id"],),
        )
    db.run(
        "UPDATE listings SET status='booked', updated_at=datetime('now') WHERE id=?",
        (listing_id,),
    )
    prop = db.one(
        "SELECT id FROM proposals WHERE listing_id=? AND status='draft' ORDER BY id DESC LIMIT 1",
        (listing_id,),
    )
    if prop:
        from . import proposals
        proposals.mark_sent(prop["id"])
    else:
        on_listing_booked(listing_id)