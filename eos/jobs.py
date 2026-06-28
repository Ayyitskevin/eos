"""SQLite-backed job queue — image derivatives, RE exports, ZIP builds."""

import json
import logging
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import brand_kits, config, db, imaging, presets

log = logging.getLogger("eos.jobs")

_pool: ThreadPoolExecutor | None = None
MAX_ATTEMPTS = 3


def _gallery_dirs(gallery_id: int) -> dict[str, Path]:
    from . import media_paths

    return {k: media_paths.gallery_subdir(gallery_id, k) for k in ("original", "web", "thumb")}


def crops_dir(gallery_id: int) -> Path:
    from . import media_paths

    return media_paths.exports_dir(gallery_id)


def zip_path(gallery_id: int, rev: int) -> Path:
    return config.ZIP_DIR / f"g{gallery_id}-r{rev}.zip"


def _h_image(p: dict) -> None:
    asset = db.one("SELECT * FROM assets WHERE id=?", (p["asset_id"],))
    if not asset:
        return
    dirs = _gallery_dirs(asset["gallery_id"])
    src = dirs["original"] / asset["stored"]
    base = Path(asset["stored"]).stem
    w, h = imaging.make_derivatives(
        str(src),
        str(dirs["web"] / f"{base}.jpg"),
        str(dirs["thumb"] / f"{base}.jpg"),
        config.WEB_MAX_PX,
        config.THUMB_MAX_PX,
        config.JPEG_QUALITY,
    )
    db.run("UPDATE assets SET status='ready', width=?, height=? WHERE id=?", (w, h, asset["id"]))


def _h_exports(p: dict) -> None:
    asset = db.one(
        "SELECT * FROM assets WHERE id=? AND kind='photo' AND status='ready'",
        (p["asset_id"],),
    )
    if not asset:
        return
    out = crops_dir(asset["gallery_id"])
    stem = Path(asset["stored"]).stem
    active = presets.active()
    if active and all((out / f"{stem}_{ps['slug']}.jpg").is_file() for ps in active):
        return
    out.mkdir(parents=True, exist_ok=True)
    src = _gallery_dirs(asset["gallery_id"])["original"] / asset["stored"]
    gal = db.one("SELECT listing_id FROM galleries WHERE id=?", (asset["gallery_id"],))
    listing_id = gal["listing_id"] if gal else None
    overlay = brand_kits.overlay_for_listing(listing_id)
    metadata = None
    if listing_id:
        from .tenant import get_site_name

        listing = db.one(
            """SELECT l.*, c.name AS client_name FROM listings l
               LEFT JOIN clients c ON c.id=l.client_id WHERE l.id=?""",
            (listing_id,),
        )
        metadata = imaging.listing_export_metadata(listing, site_name=get_site_name())
    imaging.make_crops(
        str(src),
        out,
        stem,
        config.JPEG_QUALITY,
        active,
        overlay=overlay,
        metadata=metadata,
    )


def _h_zip(p: dict) -> None:
    gid, rev = p["gallery_id"], p["rev"]
    final = zip_path(gid, rev)
    if final.exists():
        return
    assets = db.all_("SELECT * FROM assets WHERE gallery_id=? AND status='ready'", (gid,))
    src_dir = _gallery_dirs(gid)["original"]
    tmp = final.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        names: set[str] = set()
        for a in assets:
            name = a["filename"]
            if name in names:
                name = f"{Path(name).stem}_{a['id']}{Path(name).suffix}"
            names.add(name)
            zf.write(src_dir / a["stored"], arcname=name)
    tmp.rename(final)
    for old in config.ZIP_DIR.glob(f"g{gid}-r*.zip"):
        if old != final:
            old.unlink(missing_ok=True)


def _h_gallery_exports(p: dict) -> None:
    gid = p["gallery_id"]
    for row in db.all_(
        "SELECT id FROM assets WHERE gallery_id=? AND kind='photo' AND status='ready'",
        (gid,),
    ):
        _h_exports({"asset_id": row["id"]})


def _h_bundle(p: dict) -> None:
    from . import bundles

    bundles.build_bundle(p["listing_id"], p["kind"])


def _h_marketing_kit(p: dict) -> None:
    from . import marketing_kit

    try:
        marketing_kit.build_kit(p["listing_id"])
    except Exception as e:
        marketing_kit.mark_failed(p["listing_id"], str(e))
        raise


def _h_google_calendar_push(p: dict) -> None:
    from . import tenant
    from .integrations import google_calendar

    tenant.set_studio(p["studio_id"])
    google_calendar.push_appointment(p["appointment_id"])


def _h_dropbox_scan(p: dict) -> None:
    from . import tenant
    from .integrations import dropbox

    tenant.set_studio(p["studio_id"])
    dropbox.scan_now()


def _h_integration_sweep(p: dict) -> None:
    from .integrations import dropbox, google_calendar

    g = google_calendar.sweep_all()
    d = dropbox.sweep_all()
    log.info("integration sweep: google=%d dropbox=%d queued", g, d)


def _h_video_ready(p: dict) -> None:
    asset = db.one("SELECT * FROM assets WHERE id=?", (p["asset_id"],))
    if not asset:
        return
    db.run("UPDATE assets SET status='ready' WHERE id=?", (asset["id"],))


def _h_ai_cull(p: dict) -> None:
    """Stub AI cull — pick first photo per section as agent favorite."""
    gid = p["gallery_id"]
    sections = db.all_("SELECT id FROM sections WHERE gallery_id=? ORDER BY position", (gid,))
    for sec in sections:
        row = db.one(
            """SELECT id FROM assets WHERE gallery_id=? AND section_id=? AND kind='photo'
               ORDER BY position, id LIMIT 1""",
            (gid, sec["id"]),
        )
        if row:
            db.run("UPDATE assets SET agent_favorite=1 WHERE id=?", (row["id"],))


def _h_dropbox_ingest(p: dict) -> None:
    from . import tenant
    from .integrations import dropbox

    tenant.set_studio(p["studio_id"])
    try:
        dropbox.ingest_file(
            log_id=p["log_id"],
            dropbox_path=p["dropbox_path"],
            listing_id=p["listing_id"],
        )
    except Exception as e:
        db.run(
            "UPDATE dropbox_ingest_log SET status='failed', error=? WHERE id=?",
            (str(e)[:500], p["log_id"]),
        )
        raise


HANDLERS = {
    "image_derivatives": _h_image,
    "export_crops": _h_exports,
    "gallery_exports": _h_gallery_exports,
    "zip_build": _h_zip,
    "bundle_build": _h_bundle,
    "marketing_kit": _h_marketing_kit,
    "google_calendar_push": _h_google_calendar_push,
    "dropbox_ingest": _h_dropbox_ingest,
    "dropbox_scan": _h_dropbox_scan,
    "integration_sweep": _h_integration_sweep,
    "video_ready": _h_video_ready,
    "ai_cull": _h_ai_cull,
}


def enqueue(kind: str, payload: dict) -> int:
    job_id = db.run("INSERT INTO jobs (kind, payload) VALUES (?,?)", (kind, json.dumps(payload)))
    if _pool:
        _pool.submit(_execute, job_id)
    return job_id


def _claim(job_id: int):
    con = db.connect()
    try:
        cur = con.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, "
            "updated_at=datetime('now') WHERE id=? AND status='queued'",
            (job_id,),
        )
        con.commit()
        if cur.rowcount != 1:
            return None
        return con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        con.close()


def _execute(job_id: int) -> None:
    job = _claim(job_id)
    if not job:
        return
    payload = json.loads(job["payload"])
    try:
        HANDLERS[job["kind"]](payload)
        db.run(
            "UPDATE jobs SET status='done', error=NULL, updated_at=datetime('now') WHERE id=?",
            (job_id,),
        )
        log.info("job %s %s done", job_id, job["kind"])
    except Exception as e:
        status = "queued" if job["attempts"] < MAX_ATTEMPTS else "failed"
        db.run(
            "UPDATE jobs SET status=?, error=?, updated_at=datetime('now') WHERE id=?",
            (status, str(e)[:500], job_id),
        )
        log.error("job %s %s attempt %s -> %s: %s", job_id, job["kind"], job["attempts"], status, e)
        if status == "failed" and "asset_id" in payload:
            db.run("UPDATE assets SET status='failed' WHERE id=?", (payload["asset_id"],))
        if status == "queued" and _pool:
            _pool.submit(_execute, job_id)


def pending_count() -> int:
    row = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')")
    return row["n"] if row else 0


def start() -> None:
    global _pool
    db.run("UPDATE jobs SET status='queued' WHERE status='running'")
    _pool = ThreadPoolExecutor(max_workers=config.JOB_WORKERS, thread_name_prefix="eos-job")
    backlog = db.all_("SELECT id FROM jobs WHERE status='queued' ORDER BY id")
    for row in backlog:
        _pool.submit(_execute, row["id"])
    if backlog:
        log.info("re-queued %d jobs from previous run", len(backlog))


def stop() -> None:
    global _pool
    if _pool:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
