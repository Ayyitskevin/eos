"""RE pre-shoot intake — lockbox, staging, agent on-site."""

import json

from fastapi import HTTPException

from . import db, listings, security
from .vocab import QUESTIONNAIRE_FIELDS, STUDIO_ID


def get_questionnaire(q_id: int):
    row = db.one("SELECT * FROM questionnaires WHERE id=? AND studio_id=?", (q_id, STUDIO_ID))
    if not row:
        raise HTTPException(status_code=404)
    return row


def get_by_token(token: str):
    row = db.one("SELECT * FROM questionnaires WHERE token=? AND studio_id=?", (token, STUDIO_ID))
    if not row:
        raise HTTPException(status_code=404)
    return row


def list_for_listing(listing_id: int):
    listings.get_listing(listing_id)
    return db.all_(
        """SELECT * FROM questionnaires
           WHERE listing_id=? AND studio_id=? ORDER BY created_at DESC""",
        (listing_id, STUDIO_ID),
    )


def create_for_listing(listing_id: int) -> int:
    listings.get_listing(listing_id)
    qid = db.run(
        """INSERT INTO questionnaires (studio_id, listing_id, token, sent_at)
           VALUES (?,?,?,datetime('now'))""",
        (STUDIO_ID, listing_id, security.new_token()),
    )
    db.audit("admin", "questionnaire.create", f"id={qid} listing_id={listing_id}")
    return qid


def save_answers(token: str, answers: dict) -> None:
    row = get_by_token(token)
    if row["status"] == "completed":
        raise HTTPException(status_code=400, detail="already submitted")
    clean = {k: str(v).strip() for k, v in answers.items() if k in QUESTIONNAIRE_FIELDS}
    db.run(
        """UPDATE questionnaires SET status='completed', answers=?,
           completed_at=datetime('now') WHERE id=? AND studio_id=?""",
        (json.dumps(clean), row["id"], STUDIO_ID),
    )
    _apply_to_listing(row["listing_id"], clean)
    db.audit("client", "questionnaire.completed", f"listing_id={row['listing_id']}")


def _apply_to_listing(listing_id: int, answers: dict) -> None:
    """Merge questionnaire into listing access_notes and automations."""
    from . import automations

    parts = []
    for key, meta in QUESTIONNAIRE_FIELDS.items():
        val = answers.get(key, "").strip()
        if val:
            parts.append(f"{meta['label']}: {val}")
    if parts:
        listing = listings.get_listing(listing_id)
        block = "\n".join(parts)
        notes = listing["access_notes"].strip()
        merged = f"{notes}\n\n--- Pre-shoot intake ---\n{block}".strip() if notes else block
        listings.update_listing(listing_id, access_notes=merged)
    automations.on_questionnaire_completed(listing_id)
