"""Gallery delivery — room sections, PIN gate (Mise pattern, RE vocab)."""

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


def update_gallery_settings(
    gallery_id: int,
    *,
    title: str,
    client_name: str | None,
    pin: str,
    expires_at: str | None,
    published: bool,
    listing_id: int | None,
) -> None:
    get_gallery(gallery_id)
    _ensure_listing(listing_id)
    if not (pin.isdigit() and len(pin) == 4):
        raise HTTPException(status_code=400, detail="PIN must be 4 digits")
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
            listing_id,
            gallery_id,
            STUDIO_ID,
        ),
    )
    db.audit("admin", "gallery.update", f"id={gallery_id}")


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
