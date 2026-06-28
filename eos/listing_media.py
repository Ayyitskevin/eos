"""Rich media embeds per listing — Matterport, video tours, etc."""

from fastapi import HTTPException

from . import db
from .vocab import STUDIO_ID

KINDS = ("matterport", "youtube", "vimeo", "iguide", "url")


def list_for_listing(listing_id: int):
    return db.all_(
        "SELECT * FROM listing_media WHERE listing_id=? ORDER BY position, id",
        (listing_id,),
    )


def add_embed(listing_id: int, *, kind: str, label: str, embed_url: str) -> int:
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="invalid media kind")
    url = embed_url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL required")
    return db.run(
        """INSERT INTO listing_media (studio_id, listing_id, kind, label, embed_url, position)
           VALUES (?,?,?,?,?, (SELECT COALESCE(MAX(position),0)+10 FROM listing_media WHERE listing_id=?))""",
        (STUDIO_ID, listing_id, kind, label.strip(), url, listing_id),
    )


def delete_embed(embed_id: int, listing_id: int) -> None:
    db.run("DELETE FROM listing_media WHERE id=? AND listing_id=?", (embed_id, listing_id))