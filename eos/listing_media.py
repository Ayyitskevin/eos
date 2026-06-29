"""Rich media embeds per listing — Matterport, video tours, etc."""

from fastapi import HTTPException

from . import db, listings
from .vocab import STUDIO_ID

KINDS = ("matterport", "youtube", "vimeo", "iguide", "url")


def list_for_listing(listing_id: int):
    listings.get_listing(listing_id)
    return db.all_(
        """SELECT * FROM listing_media
           WHERE listing_id=? AND studio_id=? ORDER BY position, id""",
        (listing_id, STUDIO_ID),
    )


def add_embed(listing_id: int, *, kind: str, label: str, embed_url: str) -> int:
    listings.get_listing(listing_id)
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="invalid media kind")
    url = embed_url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL required")
    return db.run(
        """INSERT INTO listing_media (studio_id, listing_id, kind, label, embed_url, position)
           VALUES (?,?,?,?,?, (SELECT COALESCE(MAX(position),0)+10 FROM listing_media WHERE listing_id=? AND studio_id=?))""",
        (STUDIO_ID, listing_id, kind, label.strip(), url, listing_id, STUDIO_ID),
    )


def delete_embed(embed_id: int, listing_id: int) -> None:
    listings.get_listing(listing_id)
    db.run(
        "DELETE FROM listing_media WHERE id=? AND listing_id=? AND studio_id=?",
        (embed_id, listing_id, STUDIO_ID),
    )
