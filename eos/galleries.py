"""Gallery delivery — room sections, PIN gate (Mise pattern, RE vocab)."""

import datetime as dt

from fastapi import HTTPException

from . import db, listings, security
from .vocab import DEFAULT_SECTIONS, STUDIO_ID


def _ensure_listing(listing_id: int | None) -> None:
    if listing_id is not None:
        listings.get_listing(listing_id)


def get_gallery(gallery_id: int):
    row = db.one("SELECT * FROM galleries WHERE id=? AND studio_id=?", (gallery_id, STUDIO_ID))
    if not row:
        raise HTTPException(status_code=404)
    return row


def get_gallery_by_slug(slug: str):
    row = db.one("SELECT * FROM galleries WHERE slug=? AND studio_id=?", (slug, STUDIO_ID))
    if not row:
        raise HTTPException(status_code=404)
    return row


def require_public_gallery(gallery):
    """Enforce publication and expiry for every public gallery surface."""
    if not gallery["published"]:
        raise HTTPException(status_code=404)
    if gallery["expires_at"] and gallery["expires_at"] < dt.date.today().isoformat():
        raise HTTPException(status_code=410, detail="gallery access has expired")
    return gallery


def require_public_access(request, gallery):
    require_public_gallery(gallery)
    if not security.gallery_unlocked(request, gallery):
        raise HTTPException(status_code=403, detail="gallery access required")
    return gallery


def list_galleries():
    return db.all_(
        """SELECT g.*,
                  (SELECT COUNT(*) FROM assets a WHERE a.gallery_id=g.id) AS n_assets,
                  l.title AS listing_title,
                  l.address_line1 AS listing_address
           FROM galleries g
           LEFT JOIN listings l ON l.id=g.listing_id AND l.studio_id=g.studio_id
           WHERE g.studio_id=?
           ORDER BY g.created_at DESC""",
        (STUDIO_ID,),
    )


def create_gallery(
    title: str,
    *,
    listing_id: int | None = None,
    client_name: str | None = None,
) -> int:
    _ensure_listing(listing_id)
    slug = security.new_slug()
    pin = security.new_pin()
    token = security.new_token()
    gid = db.run(
        """INSERT INTO galleries
           (studio_id, listing_id, slug, title, client_name, pin, delivery_token)
           VALUES (?,?,?,?,?,?,?)""",
        (STUDIO_ID, listing_id, slug, title.strip(), client_name, pin, token),
    )
    for i, name in enumerate(DEFAULT_SECTIONS):
        db.run(
            "INSERT INTO sections (gallery_id, name, position) VALUES (?,?,?)",
            (gid, name, i),
        )
    db.audit("admin", "gallery.create", f"id={gid} listing_id={listing_id}")
    return gid


def gallery_sections(gallery_id: int):
    get_gallery(gallery_id)
    return db.all_(
        "SELECT * FROM sections WHERE gallery_id=? ORDER BY position",
        (gallery_id,),
    )


def gallery_assets(gallery_id: int):
    get_gallery(gallery_id)
    return db.all_(
        """SELECT * FROM assets WHERE gallery_id=?
           ORDER BY section_id, position, id""",
        (gallery_id,),
    )


def delivery_readiness(gallery_id: int, *, listing_id: int | None = None) -> dict:
    """Return the live facts that gate a one-way gallery delivery."""
    gallery = get_gallery(gallery_id)
    target_listing_id = listing_id if listing_id is not None else gallery["listing_id"]
    stats = db.one(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) AS ready,
                  SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
           FROM assets WHERE gallery_id=?""",
        (gallery_id,),
    )
    counts = {
        "total": int(stats["total"] or 0),
        "ready_assets": int(stats["ready"] or 0),
        "pending_assets": int(stats["pending"] or 0),
        "failed_assets": int(stats["failed"] or 0),
    }
    if not target_listing_id:
        return {**counts, "ready": False, "reason": "listing_required"}
    listing = listings.get_listing(target_listing_id)
    if listing["status"] in {"lead", "archived"}:
        return {**counts, "ready": False, "reason": "listing_not_ready"}
    open_shoot = db.one(
        """SELECT COUNT(*) AS n FROM appointments
           WHERE listing_id=? AND studio_id=? AND kind IN ('shoot','twilight')
             AND status NOT IN ('completed','canceled')""",
        (target_listing_id, STUDIO_ID),
    )
    if open_shoot and open_shoot["n"]:
        return {**counts, "ready": False, "reason": "shoot_not_complete"}
    if counts["total"] == 0:
        return {**counts, "ready": False, "reason": "assets_required"}
    if counts["pending_assets"] or counts["failed_assets"]:
        return {**counts, "ready": False, "reason": "assets_not_ready"}
    return {**counts, "ready": True, "reason": "ready"}


def _require_delivery_ready(gallery_id: int, listing_id: int) -> None:
    readiness = delivery_readiness(gallery_id, listing_id=listing_id)
    if not readiness["ready"]:
        raise HTTPException(
            status_code=409,
            detail=f"Delivery is not ready: {readiness['reason']}.",
        )


def _delivery_event_key(listing_id: int, delivery_round: int) -> str:
    return f"listing:{listing_id}:delivered:r{delivery_round}"


def _mark_gallery_delivered(gallery_id: int, listing_id: int) -> None:
    listing = listings.get_listing(listing_id)
    delivery_round = int(listing["revision_round"] or 0)
    db.run(
        """UPDATE galleries SET delivered_at=COALESCE(delivered_at, datetime('now'))
           WHERE id=? AND studio_id=?""",
        (gallery_id, STUDIO_ID),
    )
    db.run(
        """UPDATE listings SET status='delivered', revision_notes='',
                  delivered_at=COALESCE(delivered_at, datetime('now')),
                  updated_at=datetime('now')
           WHERE id=? AND studio_id=?""",
        (listing_id, STUDIO_ID),
    )
    _enqueue_delivery_effects(gallery_id, listing_id, delivery_round=delivery_round)


def _enqueue_delivery_effects(gallery_id: int, listing_id: int, *, delivery_round: int) -> None:
    from . import automations, delivery_notify, jobs, microsites, portal, referrals

    listing = listings.get_listing(listing_id)
    event_key = _delivery_event_key(listing_id, delivery_round)
    if listing["client_id"]:
        portal.ensure_token(listing["client_id"])
        referrals.ensure_for_client(listing["client_id"])
    microsites.ensure_site_slug(listing_id)
    microsites.maybe_auto_publish(listing_id)
    automations.on_listing_delivered(
        listing_id, gallery_id=gallery_id, delivery_round=delivery_round
    )
    delivery_notify.enqueue_gallery_email(gallery_id, event_key=event_key)
    jobs.enqueue(
        "gallery_exports",
        {"gallery_id": gallery_id, "delivery_round": delivery_round},
        idempotency_key=f"gallery:{gallery_id}:delivery-exports:r{delivery_round}",
    )


@db.transactional(immediate=True)
def update_gallery_settings(
    gallery_id: int,
    *,
    title: str,
    client_name: str | None,
    pin: str,
    expires_at: str | None,
    published: bool,
    listing_id: int | None,
) -> bool:
    old = get_gallery(gallery_id)
    target_listing_id = listing_id
    _ensure_listing(target_listing_id)
    if target_listing_id != old["listing_id"] and (old["published"] or old["delivered_at"]):
        raise HTTPException(
            status_code=409,
            detail="A delivered gallery cannot be linked to another listing; create a new gallery.",
        )
    if not (pin.isdigit() and len(pin) in (4, 6)):
        raise HTTPException(status_code=400, detail="PIN must be 4 or 6 digits")
    newly_published = bool(published and not old["published"])
    if newly_published:
        if target_listing_id is None:
            raise HTTPException(status_code=409, detail="Delivery is not ready: listing_required.")
        _require_delivery_ready(gallery_id, target_listing_id)
    db.run(
        """UPDATE galleries SET title=?, client_name=?, pin=?, expires_at=?,
           published=?, listing_id=?, content_rev=content_rev+1
           WHERE id=? AND studio_id=?""",
        (
            title.strip(),
            client_name,
            pin,
            expires_at,
            1 if published else 0,
            target_listing_id,
            gallery_id,
            STUDIO_ID,
        ),
    )
    if newly_published:
        _mark_gallery_delivered(gallery_id, target_listing_id)
    db.audit("admin", "gallery.update", f"id={gallery_id} published={int(published)}")
    return newly_published


@db.transactional(immediate=True)
def redeliver_listing(listing_id: int) -> int:
    """Complete a revision only through a still-ready published gallery."""
    listings.get_listing(listing_id)
    gallery = db.one(
        """SELECT id FROM galleries
           WHERE listing_id=? AND studio_id=? AND published=1
           ORDER BY created_at DESC, id DESC LIMIT 1""",
        (listing_id, STUDIO_ID),
    )
    if not gallery:
        raise HTTPException(
            status_code=409,
            detail="Publish a ready gallery before marking this listing delivered.",
        )
    _require_delivery_ready(gallery["id"], listing_id)
    _mark_gallery_delivered(gallery["id"], listing_id)
    db.audit("admin", "gallery.redeliver", f"gallery={gallery['id']} listing={listing_id}")
    return gallery["id"]


def toggle_agent_favorite(asset_id: int, *, gallery_id: int) -> bool:
    get_gallery(gallery_id)
    row = db.one(
        "SELECT agent_favorite FROM assets WHERE id=? AND gallery_id=?",
        (asset_id, gallery_id),
    )
    if not row:
        raise HTTPException(status_code=404)
    new_val = 0 if row["agent_favorite"] else 1
    db.run(
        "UPDATE assets SET agent_favorite=? WHERE id=? AND gallery_id=?",
        (new_val, asset_id, gallery_id),
    )
    return bool(new_val)


def agent_favorites(gallery_id: int) -> list:
    get_gallery(gallery_id)
    return db.all_(
        """SELECT * FROM assets WHERE gallery_id=? AND agent_favorite=1
           ORDER BY section_id, position, id""",
        (gallery_id,),
    )


def assets_by_section(gallery_id: int) -> tuple[list, dict, list]:
    sections = gallery_sections(gallery_id)
    assets = gallery_assets(gallery_id)
    by_section: dict[int, list] = {s["id"]: [] for s in sections}
    unsectioned = []
    for a in assets:
        if a["section_id"] and a["section_id"] in by_section:
            by_section[a["section_id"]].append(a)
        else:
            unsectioned.append(a)
    return sections, by_section, unsectioned
