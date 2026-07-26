"""SQLite-backed job queue — image derivatives, RE exports, ZIP builds."""

import json
import logging
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import brand_kits, config, db, imaging, presets, tenant

log = logging.getLogger("eos.jobs")

_pool: ThreadPoolExecutor | None = None
MAX_ATTEMPTS = 3


def _set_studio_for_gallery(gallery_id: int) -> str | None:
    studio_id = tenant.get_studio_id()
    row = db.one(
        "SELECT studio_id FROM galleries WHERE id=? AND studio_id=?",
        (gallery_id, studio_id),
    )
    return row["studio_id"] if row else None


def _set_studio_for_listing(listing_id: int) -> str | None:
    studio_id = tenant.get_studio_id()
    row = db.one(
        "SELECT studio_id FROM listings WHERE id=? AND studio_id=?",
        (listing_id, studio_id),
    )
    return row["studio_id"] if row else None


def _asset(asset_id: int, *, kind: str | None = None, status: str | None = None):
    studio_id = tenant.get_studio_id()
    sql = """SELECT a.*, g.studio_id AS studio_id
             FROM assets a JOIN galleries g ON g.id=a.gallery_id
             WHERE a.id=? AND g.studio_id=?"""
    params: list = [asset_id, studio_id]
    if kind:
        sql += " AND a.kind=?"
        params.append(kind)
    if status:
        sql += " AND a.status=?"
        params.append(status)
    return db.one(sql, tuple(params))


def _gallery_dirs(gallery_id: int) -> dict[str, Path]:
    from . import media_paths

    return {k: media_paths.gallery_subdir(gallery_id, k) for k in ("original", "web", "thumb")}


def crops_dir(gallery_id: int) -> Path:
    from . import media_paths

    return media_paths.exports_dir(gallery_id)


def zip_path(gallery_id: int, rev: int) -> Path:
    return config.ZIP_DIR / f"g{gallery_id}-r{rev}.zip"


def _sync_derivatives(gallery_id: int, dirs: dict[str, Path], base: str) -> None:
    from . import object_store, tenant

    if not object_store.enabled():
        return
    sid = tenant.get_studio_id()
    for sub, fname in (("web", f"{base}.jpg"), ("thumb", f"{base}.jpg")):
        path = dirs[sub] / fname
        if path.is_file():
            object_store.sync_gallery_file(path, studio_id=sid, gallery_id=gallery_id, sub=sub)


def _h_image(p: dict) -> None:
    asset = _asset(p["asset_id"])
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
    db.run(
        "UPDATE assets SET status='ready', width=?, height=? WHERE id=? AND gallery_id=?",
        (w, h, asset["id"], asset["gallery_id"]),
    )
    _sync_derivatives(asset["gallery_id"], dirs, base)


def _h_exports(p: dict) -> None:
    asset = _asset(p["asset_id"], kind="photo", status="ready")
    if not asset:
        return
    out = crops_dir(asset["gallery_id"])
    stem = Path(asset["stored"]).stem
    active = presets.active()
    if active and all((out / f"{stem}_{ps['slug']}.jpg").is_file() for ps in active):
        return
    out.mkdir(parents=True, exist_ok=True)
    src = _gallery_dirs(asset["gallery_id"])["original"] / asset["stored"]
    gal = db.one(
        "SELECT listing_id FROM galleries WHERE id=? AND studio_id=?",
        (asset["gallery_id"], asset["studio_id"]),
    )
    listing_id = gal["listing_id"] if gal else None
    overlay = brand_kits.overlay_for_listing(listing_id)
    metadata = None
    if listing_id:
        from .tenant import get_site_name

        listing = db.one(
            """SELECT l.*, c.name AS client_name FROM listings l
               LEFT JOIN clients c ON c.id=l.client_id AND c.studio_id=l.studio_id
               WHERE l.id=? AND l.studio_id=?""",
            (listing_id, asset["studio_id"]),
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
    if not _set_studio_for_gallery(gid):
        return
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
    if not _set_studio_for_gallery(gid):
        return
    for row in db.all_(
        "SELECT id FROM assets WHERE gallery_id=? AND kind='photo' AND status='ready'",
        (gid,),
    ):
        _h_exports({"asset_id": row["id"]})


def _h_bundle(p: dict) -> None:
    from . import bundles

    if not _set_studio_for_listing(p["listing_id"]):
        return
    bundles.build_bundle(p["listing_id"], p["kind"])


def _h_marketing_kit(p: dict) -> None:
    from . import marketing_kit

    if not _set_studio_for_listing(p["listing_id"]):
        return
    try:
        marketing_kit.build_kit(p["listing_id"])
    except Exception as e:
        marketing_kit.mark_failed(p["listing_id"], str(e))
        raise


def _require_payload_studio(payload: dict) -> str:
    studio_id = str(payload.get("studio_id") or "")
    owner = tenant.get_studio_id()
    if not studio_id or studio_id != owner:
        raise RuntimeError("job payload does not match its tenant owner")
    return owner


def _h_google_calendar_push(p: dict) -> None:
    from .integrations import google_calendar

    _require_payload_studio(p)
    google_calendar.push_appointment(p["appointment_id"])


def _h_dropbox_scan(p: dict) -> None:
    from .integrations import dropbox

    _require_payload_studio(p)
    dropbox.scan_now()


def _h_integration_sweep(p: dict) -> None:
    from .integrations import dropbox, google_calendar

    g = google_calendar.sweep_all()
    d = dropbox.sweep_all()
    log.info("integration sweep: google=%d dropbox=%d queued", g, d)


def _h_video_ready(p: dict) -> None:
    asset = _asset(p["asset_id"])
    if not asset:
        return
    db.run(
        "UPDATE assets SET status='ready' WHERE id=? AND gallery_id=?",
        (asset["id"], asset["gallery_id"]),
    )


def _h_ai_cull(p: dict) -> None:
    """Stub AI cull — pick first photo per section as agent favorite."""
    gid = p["gallery_id"]
    if not _set_studio_for_gallery(gid):
        return
    sections = db.all_("SELECT id FROM sections WHERE gallery_id=? ORDER BY position", (gid,))
    for sec in sections:
        row = db.one(
            """SELECT id FROM assets WHERE gallery_id=? AND section_id=? AND kind='photo'
               ORDER BY position, id LIMIT 1""",
            (gid, sec["id"]),
        )
        if row:
            db.run(
                "UPDATE assets SET agent_favorite=1 WHERE id=? AND gallery_id=?",
                (row["id"], gid),
            )


def _h_dropbox_ingest(p: dict) -> None:
    from .integrations import dropbox

    _require_payload_studio(p)
    dropbox.ingest_file(
        log_id=p["log_id"],
        dropbox_path=p["dropbox_path"],
        listing_id=p["listing_id"],
    )


def _h_geocode_listing(p: dict) -> None:
    listing_id = p["listing_id"]
    if not _set_studio_for_listing(listing_id):
        return
    from . import drive_time

    drive_time.geocode_listing(listing_id)


HANDLERS = {
    "geocode_listing": _h_geocode_listing,
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


def _submit(job_id: int) -> None:
    if _pool:
        _pool.submit(_execute, job_id)


def _payload_studio_candidates(payload: dict) -> set[str]:
    candidates: set[str] = set()
    explicit = str(payload.get("studio_id") or "").strip()
    if explicit:
        candidates.add(explicit)
    ownership_queries = (
        (
            "asset_id",
            "SELECT g.studio_id FROM assets a JOIN galleries g ON g.id=a.gallery_id WHERE a.id=?",
        ),
        ("gallery_id", "SELECT studio_id FROM galleries WHERE id=?"),
        ("listing_id", "SELECT studio_id FROM listings WHERE id=?"),
        ("log_id", "SELECT studio_id FROM dropbox_ingest_log WHERE id=?"),
    )
    for key, sql in ownership_queries:
        if payload.get(key) is None:
            continue
        row = db.one(sql, (payload[key],))
        if row:
            candidates.add(row["studio_id"])
    return candidates


def enqueue(kind: str, payload: dict, *, idempotency_key: str | None = None) -> int:
    """Persist a tenant-owned job once, then wake workers after commit."""
    studio_id = tenant.get_studio_id()
    candidates = _payload_studio_candidates(payload)
    if candidates and candidates != {studio_id}:
        raise ValueError("job payload does not belong to the active studio")
    key = (idempotency_key or "").strip() or None
    if key and len(key) > 200:
        raise ValueError("invalid job idempotency key")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with db.tx() as con:
        if key:
            cur = con.execute(
                """INSERT OR IGNORE INTO jobs
                   (studio_id, kind, payload, idempotency_key) VALUES (?,?,?,?)""",
                (studio_id, kind, encoded, key),
            )
            if cur.rowcount:
                job_id = cur.lastrowid
                status = "queued"
            else:
                existing = con.execute(
                    """SELECT id, status FROM jobs
                       WHERE studio_id=? AND kind=? AND idempotency_key=?""",
                    (studio_id, kind, key),
                ).fetchone()
                if not existing:
                    raise RuntimeError("job deduplication failed")
                job_id = existing["id"]
                status = existing["status"]
        else:
            cur = con.execute(
                "INSERT INTO jobs (studio_id, kind, payload) VALUES (?,?,?)",
                (studio_id, kind, encoded),
            )
            job_id = cur.lastrowid
            status = "queued"
        if status == "queued":
            db.after_commit(lambda jid=job_id: _submit(jid))
        return job_id


def _legacy_job_owner(kind: str, payload: dict) -> str | None:
    candidates = _payload_studio_candidates(payload)
    if not candidates and kind == "integration_sweep":
        candidates.add("default")
    if len(candidates) != 1:
        return None
    studio_id = candidates.pop()
    return studio_id if db.one("SELECT id FROM studio WHERE id=?", (studio_id,)) else None


def _claim(job_id: int):
    row = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not row or row["status"] != "queued":
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        payload = {}
    studio_id = row["studio_id"] or _legacy_job_owner(row["kind"], payload)
    if not studio_id:
        db.run(
            """UPDATE jobs SET status='failed', attempts=attempts+1,
                      error='job tenant ownership unavailable', updated_at=datetime('now')
               WHERE id=? AND studio_id IS NULL AND status='queued'""",
            (job_id,),
        )
        log.error("job %s blocked: tenant ownership unavailable", job_id)
        return None
    if row["studio_id"] is None:
        db.run(
            "UPDATE jobs SET studio_id=? WHERE id=? AND studio_id IS NULL",
            (studio_id, job_id),
        )
    con = db.connect()
    try:
        cur = con.execute(
            """UPDATE jobs SET status='running', attempts=attempts+1,
                      updated_at=datetime('now')
               WHERE id=? AND studio_id=? AND status='queued'""",
            (job_id, studio_id),
        )
        con.commit()
        if cur.rowcount != 1:
            return None
        return con.execute(
            "SELECT * FROM jobs WHERE id=? AND studio_id=?", (job_id, studio_id)
        ).fetchone()
    finally:
        con.close()


def _execute(job_id: int) -> None:
    job = _claim(job_id)
    if not job:
        return
    previous_studio = tenant.get_studio_id()
    studio_id = job["studio_id"]
    tenant.set_studio(studio_id)
    payload: dict = {}
    retry = False
    try:
        payload = json.loads(job["payload"])
        candidates = _payload_studio_candidates(payload)
        if candidates and candidates != {studio_id}:
            raise RuntimeError("job payload does not match its tenant owner")
        HANDLERS[job["kind"]](payload)
        db.run(
            """UPDATE jobs SET status='done', error=NULL, updated_at=datetime('now')
               WHERE id=? AND studio_id=?""",
            (job_id, studio_id),
        )
        log.info("job %s %s done", job_id, job["kind"])
    except Exception as e:
        status = "queued" if job["attempts"] < MAX_ATTEMPTS else "failed"
        db.run(
            """UPDATE jobs SET status=?, error=?, updated_at=datetime('now')
               WHERE id=? AND studio_id=?""",
            (status, str(e)[:500], job_id, studio_id),
        )
        log.error(
            "job %s %s attempt %s -> %s: %s",
            job_id,
            job["kind"],
            job["attempts"],
            status,
            e,
        )
        if status == "failed" and "asset_id" in payload:
            asset = db.one(
                """SELECT a.gallery_id FROM assets a
                   JOIN galleries g ON g.id=a.gallery_id
                   WHERE a.id=? AND g.studio_id=?""",
                (payload["asset_id"], studio_id),
            )
            if asset:
                db.run(
                    "UPDATE assets SET status='failed' WHERE id=? AND gallery_id=?",
                    (payload["asset_id"], asset["gallery_id"]),
                )
        retry = status == "queued"
    finally:
        tenant.set_studio(previous_studio)
    if retry:
        _submit(job_id)


def _backfill_failed_job_owners(limit: int = 200) -> None:
    rows = db.all_(
        """SELECT id, kind, payload FROM jobs
           WHERE studio_id IS NULL AND status='failed' ORDER BY id LIMIT ?""",
        (limit,),
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        studio_id = _legacy_job_owner(row["kind"], payload)
        if studio_id:
            db.run(
                "UPDATE jobs SET studio_id=? WHERE id=? AND studio_id IS NULL",
                (studio_id, row["id"]),
            )


def list_failed(limit: int = 20):
    _backfill_failed_job_owners()
    return db.all_(
        """SELECT id, kind, attempts, error, created_at, updated_at
           FROM jobs WHERE studio_id=? AND status='failed'
           ORDER BY updated_at DESC, id DESC LIMIT ?""",
        (tenant.get_studio_id(), limit),
    )


def retry_job(job_id: int) -> bool:
    studio_id = tenant.get_studio_id()
    row = db.one(
        "SELECT id FROM jobs WHERE id=? AND studio_id=?",
        (job_id, studio_id),
    )
    if not row:
        return False
    with db.tx(immediate=True) as con:
        cur = con.execute(
            """UPDATE jobs SET status='queued', attempts=0, error=NULL,
                      updated_at=datetime('now')
               WHERE id=? AND studio_id=? AND status='failed'""",
            (job_id, studio_id),
        )
        if cur.rowcount:
            db.audit("admin", "job.retry", f"job={job_id}")
            db.after_commit(lambda jid=job_id: _submit(jid))
        return cur.rowcount == 1


def failed_count() -> int:
    row = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status='failed'")
    return row["n"] if row else 0


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
