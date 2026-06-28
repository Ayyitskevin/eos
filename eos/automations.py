"""Lightweight listing status hooks — solo-operator guardrails."""

import logging

from . import db

log = logging.getLogger("eos.automations")


def on_questionnaire_completed(listing_id: int) -> None:
    row = db.one("SELECT status FROM listings WHERE id=?", (listing_id,))
    if row and row["status"] == "booked":
        db.run("UPDATE listings SET status='shooting', updated_at=datetime('now') WHERE id=?", (listing_id,))
        log.info("listing %s auto-advanced booked → shooting (questionnaire)", listing_id)


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


def on_invoice_paid(listing_id: int | None) -> None:
    if not listing_id:
        return
    db.run(
        "UPDATE listing_tasks SET done=1 WHERE listing_id=? AND label LIKE '%invoice%'",
        (listing_id,),
    )