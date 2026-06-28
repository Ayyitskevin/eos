"""Agent watermark kits — hierarchy-aware overlay resolution."""

from . import clients, config, db


def _kit_spec(owner_id: int, kit) -> dict | None:
    path = config.BRAND_DIR / str(owner_id) / kit["stored"]
    if not path.is_file():
        return None
    return {
        "path": str(path),
        "position": kit["position"],
        "opacity": kit["opacity"],
        "scale_pct": kit["scale_pct"],
        "margin_pct": kit["margin_pct"],
    }


def overlay_for_client(client_id: int | None) -> dict | None:
    if not client_id:
        return None
    for owner_id in (client_id, *clients.ancestor_ids(client_id)):
        kit = db.one(
            "SELECT * FROM brand_kits WHERE client_id=? AND active=1 ORDER BY id DESC LIMIT 1",
            (owner_id,),
        )
        if kit:
            spec = _kit_spec(owner_id, kit)
            if spec:
                return spec
    return None


def overlay_for_listing(listing_id: int | None) -> dict | None:
    if not listing_id:
        return None
    row = db.one("SELECT client_id FROM listings WHERE id=?", (listing_id,))
    return overlay_for_client(row["client_id"] if row else None)


def get_kit(client_id: int):
    return db.one(
        "SELECT * FROM brand_kits WHERE client_id=? ORDER BY id DESC LIMIT 1",
        (client_id,),
    )
