"""Property microsites — public listing pages at /l/{slug}."""

from fastapi import HTTPException

from . import clients, config, db, galleries, listing_media, listings, security, studio
from .vocab import STUDIO_ID


def ensure_site_slug(listing_id: int) -> str:
    row = listings.get_listing(listing_id)
    if row["site_slug"]:
        return row["site_slug"]
    slug = security.new_slug(12)
    db.run("UPDATE listings SET site_slug=? WHERE id=?", (slug, listing_id))
    return slug


def get_published_by_slug(slug: str):
    row = db.one(
        """SELECT * FROM listings
           WHERE site_slug=? AND studio_id=? AND site_published=1""",
        (slug, STUDIO_ID),
    )
    if not row:
        raise HTTPException(status_code=404)
    return row


def site_url(listing_id: int) -> str:
    slug = ensure_site_slug(listing_id)
    return f"{config.BASE_URL}/l/{slug}"


def update_site(
    listing_id: int,
    *,
    published: bool | None = None,
    description: str | None = None,
    lead_capture: bool | None = None,
) -> None:
    listings.get_listing(listing_id)
    ensure_site_slug(listing_id)
    parts = ["updated_at=datetime('now')"]
    params: list = []
    if published is not None:
        parts.append("site_published=?")
        params.append(1 if published else 0)
    if description is not None:
        parts.append("site_description=?")
        params.append(description.strip())
    if lead_capture is not None:
        parts.append("site_lead_capture=?")
        params.append(1 if lead_capture else 0)
    if len(parts) == 1:
        return
    params.append(listing_id)
    db.run(f"UPDATE listings SET {', '.join(parts)} WHERE id=?", tuple(params))
    db.audit("admin", "listing.site", f"id={listing_id}")


def maybe_auto_publish(listing_id: int) -> None:
    profile = studio.get_profile()
    if not profile["auto_publish_site"]:
        return
    ensure_site_slug(listing_id)
    db.run(
        "UPDATE listings SET site_published=1, updated_at=datetime('now') WHERE id=?",
        (listing_id,),
    )


def primary_gallery(listing_id: int):
    return db.one(
        """SELECT * FROM galleries
           WHERE listing_id=? AND studio_id=? AND published=1
           ORDER BY created_at DESC LIMIT 1""",
        (listing_id, STUDIO_ID),
    )


def site_context(listing_row) -> dict:
    listing_id = listing_row["id"]
    gallery = primary_gallery(listing_id)
    client = None
    if listing_row["client_id"]:
        client = clients.get_client(listing_row["client_id"])
    studio_row = studio.get_studio()
    sections, by_section, unsectioned = ([], {}, [])
    assets = []
    cover_url = None
    if gallery:
        sections, by_section, unsectioned = galleries.assets_by_section(gallery["id"])
        assets = [a for a in galleries.gallery_assets(gallery["id"]) if a["status"] == "ready"]
        if gallery["cover_asset_id"]:
            cover_url = f"/l/{listing_row['site_slug']}/hero.jpg"
        elif assets:
            cover_url = f"/l/{listing_row['site_slug']}/hero.jpg"
    embeds = listing_media.list_for_listing(listing_id)
    return {
        "listing": listing_row,
        "address": listings.format_address(listing_row),
        "gallery": gallery,
        "sections": sections,
        "by_section": by_section,
        "unsectioned": unsectioned,
        "assets": assets,
        "embeds": embeds,
        "client": client,
        "studio": studio_row,
        "cover_url": cover_url,
        "site_url": f"{config.BASE_URL}/l/{listing_row['site_slug']}",
    }
