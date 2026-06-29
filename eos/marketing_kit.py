"""Marketing kit state and build orchestration."""

from pathlib import Path

from . import config, db, jobs, listings, microsites, studio
from .vocab import STUDIO_ID

KIT_FILES = ("ig_square", "ig_story", "flyer")


def kit_dir(listing_id: int) -> Path:
    return config.DATA_DIR / "marketing" / str(listing_id)


def _file_path(listing_id: int, kind: str) -> Path:
    ext = "pdf" if kind == "flyer" else "jpg"
    return kit_dir(listing_id) / f"{kind}.{ext}"


def get_status(listing_id: int) -> dict:
    listings.get_listing(listing_id)
    row = db.one(
        "SELECT * FROM listing_marketing_kit WHERE listing_id=? AND studio_id=?",
        (listing_id, STUDIO_ID),
    )
    if not row:
        return {"status": "pending", "ig_square": "", "ig_story": "", "flyer": ""}
    return dict(row)


def _cover_path(listing_id: int) -> str | None:
    gal = microsites.primary_gallery(listing_id)
    if not gal:
        return None
    asset_id = gal["cover_asset_id"]
    if not asset_id:
        row = db.one(
            "SELECT * FROM assets WHERE gallery_id=? AND status='ready' ORDER BY position, id LIMIT 1",
            (gal["id"],),
        )
        if not row:
            return None
        asset_id = row["id"]
    asset = db.one("SELECT * FROM assets WHERE id=? AND gallery_id=?", (asset_id, gal["id"]))
    if not asset:
        return None
    from . import media_paths

    base = media_paths.gallery_dir(gal["id"])
    path = base / "web" / f"{Path(asset['stored']).stem}.jpg"
    if not path.is_file():
        path = base / "original" / asset["stored"]
    return str(path) if path.is_file() else None


def build_kit(listing_id: int) -> None:
    from . import clients, marketing_graphics

    listing = listings.get_listing(listing_id)
    cover = _cover_path(listing_id)
    if not cover:
        raise RuntimeError("no cover image — publish a gallery with photos first")

    headline = listings.format_address(listing) or listing["title"]
    parts = []
    if listing["beds"]:
        parts.append(
            f"{listing['beds']:.0f} bed"
            if listing["beds"] == int(listing["beds"])
            else f"{listing['beds']} bed"
        )
    if listing["baths"]:
        parts.append(f"{listing['baths']} bath")
    if listing["sqft"]:
        parts.append(f"{listing['sqft']:,} sqft")
    specs = " · ".join(parts)
    subline = listing["site_description"] or listing["title"]

    agent_line = ""
    if listing["client_id"]:
        c = clients.get_client(listing["client_id"])
        agent_line = c["name"]
        if c["company"]:
            agent_line = f"{agent_line} · {c['company']}"

    studio_row = studio.get_studio()
    studio_line = studio_row["name"]
    if studio_row["contact_email"]:
        studio_line = f"{studio_line} · {studio_row['contact_email']}"

    kit_dir(listing_id)
    ig_sq = _file_path(listing_id, "ig_square")
    ig_st = _file_path(listing_id, "ig_story")
    flyer = _file_path(listing_id, "flyer")

    marketing_graphics.build_ig_square(cover, headline, subline, ig_sq)
    marketing_graphics.build_ig_story(cover, headline, subline, ig_st)
    marketing_graphics.build_flyer(
        cover,
        headline=headline,
        subline=subline,
        specs=specs,
        agent_line=agent_line,
        studio_line=studio_line,
        out_path=flyer,
    )

    db.run(
        """INSERT INTO listing_marketing_kit
           (listing_id, studio_id, status, ig_square, ig_story, flyer, error, updated_at)
           VALUES (?,?,?,?,?,?,?,datetime('now'))
           ON CONFLICT(listing_id) DO UPDATE SET
             status='ready', ig_square=excluded.ig_square, ig_story=excluded.ig_story,
             flyer=excluded.flyer, error='', updated_at=datetime('now')""",
        (listing_id, STUDIO_ID, "ready", str(ig_sq), str(ig_st), str(flyer), ""),
    )


def mark_building(listing_id: int) -> None:
    db.run(
        """INSERT INTO listing_marketing_kit (listing_id, studio_id, status, updated_at)
           VALUES (?,?,?,datetime('now'))
           ON CONFLICT(listing_id) DO UPDATE SET status='building', error='', updated_at=datetime('now')""",
        (listing_id, STUDIO_ID, "building"),
    )


def mark_failed(listing_id: int, error: str) -> None:
    db.run(
        """INSERT INTO listing_marketing_kit (listing_id, studio_id, status, error, updated_at)
           VALUES (?,?,?,?,datetime('now'))
           ON CONFLICT(listing_id) DO UPDATE SET status='failed', error=excluded.error, updated_at=datetime('now')""",
        (listing_id, STUDIO_ID, "failed", error[:500]),
    )


def enqueue_build(listing_id: int) -> int:
    listings.get_listing(listing_id)
    mark_building(listing_id)
    return jobs.enqueue("marketing_kit", {"listing_id": listing_id})


def asset_path(listing_id: int, kind: str) -> Path | None:
    if kind not in KIT_FILES:
        return None
    row = get_status(listing_id)
    if row["status"] != "ready":
        return None
    stored = row.get(kind) or ""
    path = Path(stored) if stored else _file_path(listing_id, kind)
    return path if path.is_file() else None
