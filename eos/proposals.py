"""Listing proposals — RE package presets, line items, accept/decline."""

import json

from fastapi import HTTPException

from . import db, security, studio
from .vocab import STUDIO_ID

MAX_ITEM_ROWS = 12


def get_proposal(proposal_id: int):
    row = db.one("SELECT * FROM proposals WHERE id=? AND studio_id=?", (proposal_id, STUDIO_ID))
    if not row:
        raise HTTPException(status_code=404)
    return row


def get_proposal_by_slug(slug: str):
    row = db.one("SELECT * FROM proposals WHERE slug=? AND studio_id=?", (slug, STUDIO_ID))
    if not row or row["status"] == "draft":
        raise HTTPException(status_code=404)
    return row


def list_for_listing(listing_id: int):
    return db.all_(
        "SELECT * FROM proposals WHERE listing_id=? ORDER BY created_at DESC",
        (listing_id,),
    )


def package_presets() -> dict[str, dict]:
    """Build proposal presets from service_packages rows."""
    presets = {"blank": {"title": "Proposal", "items": [], "intro": ""}}
    for pkg in studio.list_packages():
        key = pkg["name"].lower().replace(" ", "_")
        presets[key] = {
            "title": f"{pkg['name']} — Real Estate Photography",
            "intro": pkg["description"],
            "items": [
                {"label": pkg["name"], "qty": 1, "unit_cents": pkg["price_cents"]},
                {"label": f"{pkg['turnaround_hours']}hr turnaround", "qty": 1, "unit_cents": 0},
                {"label": "MLS + Zillow export sizes", "qty": 1, "unit_cents": 0},
                {"label": "Private online gallery delivery", "qty": 1, "unit_cents": 0},
            ],
        }
    return presets


def parse_items(form) -> tuple[str, int]:
    items, total = [], 0
    for i in range(MAX_ITEM_ROWS):
        label = (form.get(f"item_label_{i}") or "").strip()
        if not label:
            continue
        try:
            qty = max(1, int(form.get(f"item_qty_{i}") or "1"))
            unit_cents = round(float(form.get(f"item_price_{i}") or "0") * 100)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"bad numbers on row {i + 1}") from e
        items.append({"label": label, "qty": qty, "unit_cents": unit_cents})
        total += qty * unit_cents
    return json.dumps(items), total


def create_proposal(listing_id: int, *, preset: str = "blank") -> int:
    presets = package_presets()
    tpl = presets.get(preset, presets["blank"])
    pid = db.run(
        """INSERT INTO proposals
           (studio_id, listing_id, slug, title, intro, line_items, total_cents)
           VALUES (?,?,?,?,?,?,?)""",
        (
            STUDIO_ID,
            listing_id,
            security.new_slug(),
            tpl["title"],
            tpl.get("intro", ""),
            json.dumps(tpl["items"]),
            sum(i["qty"] * i["unit_cents"] for i in tpl["items"]),
        ),
    )
    db.audit("admin", "proposal.create", f"id={pid} listing_id={listing_id}")
    return pid


def update_proposal(
    proposal_id: int, *, title: str, intro: str, line_items: str, total_cents: int
) -> None:
    prop = get_proposal(proposal_id)
    if prop["status"] != "draft":
        raise HTTPException(status_code=400, detail="sent proposals are locked")
    db.run(
        "UPDATE proposals SET title=?, intro=?, line_items=?, total_cents=? WHERE id=?",
        (title.strip(), intro.strip(), line_items, total_cents, proposal_id),
    )


def mark_sent(proposal_id: int) -> None:
    prop = get_proposal(proposal_id)
    if prop["status"] != "draft":
        raise HTTPException(status_code=400, detail="already sent")
    db.run(
        "UPDATE proposals SET status='sent', sent_at=datetime('now') WHERE id=?",
        (proposal_id,),
    )
    db.run(
        "UPDATE listings SET status='booked' WHERE id=? AND status='lead'",
        (prop["listing_id"],),
    )
    from . import automations

    automations.on_proposal_sent(prop["listing_id"])
    row = db.one("SELECT status FROM listings WHERE id=?", (prop["listing_id"],))
    if row and row["status"] == "booked":
        automations.on_listing_booked(prop["listing_id"])


def mark_viewed(proposal_id: int) -> None:
    db.run(
        "UPDATE proposals SET viewed_at=datetime('now') WHERE id=? AND viewed_at IS NULL",
        (proposal_id,),
    )


def accept_by_slug(slug: str) -> None:
    row = get_proposal_by_slug(slug)
    if row["status"] != "sent":
        raise HTTPException(status_code=400, detail="proposal is not open for acceptance")
    db.run(
        "UPDATE proposals SET status='accepted', accepted_at=datetime('now') WHERE id=?",
        (row["id"],),
    )
    db.run(
        "UPDATE listings SET status='booked' WHERE id=? AND status IN ('lead','booked')",
        (row["listing_id"],),
    )
    from . import automations

    automations.on_listing_booked(row["listing_id"])
    from . import automations

    automations.on_listing_booked(row["listing_id"])


def decline_by_slug(slug: str) -> None:
    row = get_proposal_by_slug(slug)
    if row["status"] != "sent":
        raise HTTPException(status_code=400, detail="proposal is not open")
    db.run("UPDATE proposals SET status='declined' WHERE id=?", (row["id"],))
