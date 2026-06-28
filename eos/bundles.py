"""MLS / Zillow / full-res download bundles for property sites."""

import zipfile
from pathlib import Path

from . import config, db, jobs, presets
from .galleries import get_gallery
from .jobs import crops_dir
from .vocab import STUDIO_ID

BUNDLE_KINDS = ("mls", "zillow", "fullres")

_PRESET_SLUGS = {
    "mls": "mls-3x2",
    "zillow": "zillow-16x9",
}


def bundle_path(listing_id: int, kind: str, gallery_id: int, rev: int) -> Path:
    return config.ZIP_DIR / f"l{listing_id}-{kind}-g{gallery_id}-r{rev}.zip"


def _preset_for_kind(kind: str):
    slug = _PRESET_SLUGS.get(kind)
    if not slug:
        return None
    return db.one(
        "SELECT * FROM crop_presets WHERE studio_id=? AND slug=? AND active=1",
        (STUDIO_ID, slug),
    )


def build_bundle(listing_id: int, kind: str) -> Path | None:
    if kind not in BUNDLE_KINDS:
        return None
    gal = db.one(
        """SELECT * FROM galleries
           WHERE listing_id=? AND studio_id=? AND published=1
           ORDER BY created_at DESC LIMIT 1""",
        (listing_id, STUDIO_ID),
    )
    if not gal:
        return None
    final = bundle_path(listing_id, kind, gal["id"], gal["content_rev"])
    if final.is_file():
        return final

    assets = db.all_(
        "SELECT * FROM assets WHERE gallery_id=? AND status='ready' ORDER BY position, id",
        (gal["id"],),
    )
    if not assets:
        return None

    tmp = final.with_suffix(".part")
    src_exports = crops_dir(gal["id"])
    src_original = config.MEDIA_DIR / str(gal["id"]) / "original"

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        names: set[str] = set()
        if kind == "fullres":
            for a in assets:
                name = a["filename"]
                if name in names:
                    name = f"{Path(name).stem}_{a['id']}{Path(name).suffix}"
                names.add(name)
                path = src_original / a["stored"]
                if path.is_file():
                    zf.write(path, arcname=name)
        else:
            preset = _preset_for_kind(kind)
            if not preset:
                return None
            stem_slug = preset["slug"]
            for a in assets:
                stem = Path(a["stored"]).stem
                crop_name = f"{stem}_{stem_slug}.jpg"
                path = src_exports / crop_name
                if not path.is_file():
                    continue
                arc = f"{stem}_{kind}.jpg"
                if arc in names:
                    arc = f"{stem}_{kind}_{a['id']}.jpg"
                names.add(arc)
                zf.write(path, arcname=arc)

    if not names:
        tmp.unlink(missing_ok=True)
        return None
    tmp.rename(final)
    for old in config.ZIP_DIR.glob(f"l{listing_id}-{kind}-g{gal['id']}-r*.zip"):
        if old != final:
            old.unlink(missing_ok=True)
    return final


def get_ready_bundle(listing_id: int, kind: str) -> Path | None:
    gal = db.one(
        """SELECT id, content_rev FROM galleries
           WHERE listing_id=? AND studio_id=? AND published=1
           ORDER BY created_at DESC LIMIT 1""",
        (listing_id, STUDIO_ID),
    )
    if not gal:
        return None
    path = bundle_path(listing_id, kind, gal["id"], gal["content_rev"])
    return path if path.is_file() else None


def enqueue_bundle(listing_id: int, kind: str) -> int:
    return jobs.enqueue("bundle_build", {"listing_id": listing_id, "kind": kind})


def ensure_exports(gallery_id: int) -> None:
    get_gallery(gallery_id)
    active = presets.active()
    if not active:
        return
    assets = db.all_(
        "SELECT id FROM assets WHERE gallery_id=? AND kind='photo' AND status='ready'",
        (gallery_id,),
    )
    for row in assets:
        jobs.enqueue("export_crops", {"asset_id": row["id"]})