"""Dropbox watch-folder ingest — auto-import photos into listing galleries."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse
import uuid
from pathlib import Path

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import config, db, integration_events, jobs, oauth_store, studio
from ..imaging import PHOTO_EXTS
from ..vocab import STUDIO_ID

log = logging.getLogger("eos.integrations.dropbox")

AUTH_URL = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
PROVIDER = "dropbox"
_STATE_MAX_AGE = 600
_PHOTO_RE = re.compile(r"\.(jpe?g|png|heic|heif|webp|tif{1,2})$", re.I)


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="eos-dropbox-oauth")


def is_configured() -> bool:
    return bool(
        config.DROPBOX_APP_KEY and config.DROPBOX_APP_SECRET and config.DROPBOX_REDIRECT_URI
    )


def is_connected() -> bool:
    return oauth_store.get_connection(PROVIDER) is not None


def is_enabled() -> bool:
    profile = studio.get_profile()
    return bool(profile["dropbox_enabled"]) and is_connected()


def connect_url() -> str:
    state = _signer().dumps({"studio_id": STUDIO_ID})
    params = {
        "client_id": config.DROPBOX_APP_KEY,
        "redirect_uri": config.DROPBOX_REDIRECT_URI,
        "response_type": "code",
        "token_access_type": "offline",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def handle_callback(*, code: str, state: str) -> None:
    try:
        payload = _signer().loads(state, max_age=_STATE_MAX_AGE)
    except BadSignature as e:
        raise ValueError("Invalid OAuth state") from e
    from .. import tenant

    tenant.set_studio(payload["studio_id"])
    data = oauth_store.exchange_code(
        token_url=TOKEN_URL,
        client_id=config.DROPBOX_APP_KEY,
        client_secret=config.DROPBOX_APP_SECRET,
        code=code,
        redirect_uri=config.DROPBOX_REDIRECT_URI,
    )
    oauth_store.save_tokens(
        PROVIDER,
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_in=data.get("expires_in"),
        scopes="files.metadata.read files.content.read",
        account_label="Dropbox",
    )
    studio.update_profile(dropbox_enabled=True)
    db.audit("admin", "integration.dropbox.connect", None)


def disconnect() -> None:
    oauth_store.delete_connection(PROVIDER)
    db.run("DELETE FROM dropbox_sync_state WHERE studio_id=?", (STUDIO_ID,))
    studio.update_profile(dropbox_enabled=False)
    db.audit("admin", "integration.dropbox.disconnect", None)


def _token() -> str | None:
    tok = oauth_store.access_token(PROVIDER)
    if tok:
        return tok
    return oauth_store.refresh_access(
        PROVIDER,
        token_url=TOKEN_URL,
        client_id=config.DROPBOX_APP_KEY,
        client_secret=config.DROPBOX_APP_SECRET,
    )


def _watch_path() -> str:
    profile = studio.get_profile()
    path = (profile["dropbox_watch_path"] or "/Eos Ingest").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/Eos Ingest"


def resolve_listing_id(dropbox_path: str) -> int | None:
    watch = _watch_path()
    if not dropbox_path.startswith(watch):
        return None
    rel = dropbox_path[len(watch) :].lstrip("/")
    if not rel:
        return None
    parts = rel.split("/")
    if len(parts) >= 2 and parts[0].isdigit():
        lid = int(parts[0])
        row = db.one("SELECT id FROM listings WHERE id=? AND studio_id=?", (lid, STUDIO_ID))
        return row["id"] if row else None
    profile = studio.get_profile()
    default_id = profile["dropbox_default_listing_id"]
    if default_id:
        row = db.one("SELECT id FROM listings WHERE id=? AND studio_id=?", (default_id, STUDIO_ID))
        return row["id"] if row else None
    return None


def _gallery_for_listing(listing_id: int) -> int:
    """Get or create one listing gallery under a serialized DB reservation."""
    with db.tx(immediate=True) as con:
        row = con.execute(
            "SELECT id FROM galleries WHERE listing_id=? AND studio_id=? ORDER BY id LIMIT 1",
            (listing_id, str(STUDIO_ID)),
        ).fetchone()
        if row:
            return row["id"]
        from .. import galleries

        listing = con.execute(
            "SELECT title FROM listings WHERE id=? AND studio_id=?",
            (listing_id, str(STUDIO_ID)),
        ).fetchone()
        if not listing:
            raise RuntimeError("listing not found for this studio")
        return galleries.create_gallery(listing["title"], listing_id=listing_id)


def _entry_identity(entry: dict, path: str) -> dict:
    file_id = str(entry.get("id") or "").strip()
    revision = str(entry.get("rev") or "").strip()
    content_hash = str(entry.get("content_hash") or "").strip()
    path_lower = str(entry.get("path_lower") or path).strip().lower()
    if not file_id or not revision or not path_lower:
        raise RuntimeError(f"Dropbox file identity is incomplete for {path}")
    provider_key = hashlib.sha256(f"{file_id}\0{revision}".encode()).hexdigest()
    return {
        "file_id": file_id,
        "revision": revision,
        "content_hash": content_hash,
        "path_lower": path_lower,
        "provider_key": provider_key,
    }


def _record_scan_failure(error: Exception) -> None:
    try:
        integration_events.set_sync_status("dropbox", ok=False, error=str(error))
        integration_events.log_event("dropbox", "scan.failed", detail=str(error), ok=False)
    except Exception:
        log.exception("dropbox scan failure could not be recorded")


def list_ingest_logs(*, limit: int = 50):
    """Return provider identity and recovery state for the active studio."""
    bounded = max(1, min(int(limit), 100))
    return db.all_(
        """SELECT id, dropbox_path, dropbox_path_lower, provider_file_id,
                  provider_revision, provider_content_hash, listing_id, asset_id,
                  job_id, stored, status, attempts, claimed_at, error,
                  created_at, updated_at
           FROM dropbox_ingest_log
           WHERE studio_id=?
           ORDER BY created_at DESC, id DESC
           LIMIT ?""",
        (str(STUDIO_ID), bounded),
    )


def scan_folder() -> int:
    if not is_enabled():
        return 0
    token = _token()
    if not token:
        raise RuntimeError(f"dropbox token unavailable for {STUDIO_ID}")
    watch = _watch_path()
    state = db.one("SELECT cursor FROM dropbox_sync_state WHERE studio_id=?", (STUDIO_ID,))
    cursor = state["cursor"] if state else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        if cursor:
            resp = httpx.post(
                "https://api.dropboxapi.com/2/files/list_folder/continue",
                headers=headers,
                json={"cursor": cursor},
                timeout=60,
            )
        else:
            resp = httpx.post(
                "https://api.dropboxapi.com/2/files/list_folder",
                headers=headers,
                json={"path": watch, "recursive": True, "include_deleted": False},
                timeout=60,
            )
        resp.raise_for_status()
        data = resp.json()
        candidates: list[tuple[str, int, dict]] = []
        for entry in data.get("entries", []):
            if entry.get(".tag") != "file":
                continue
            dropbox_path = entry.get("path_display") or entry.get("path_lower", "")
            if not _PHOTO_RE.search(dropbox_path):
                continue
            identity = _entry_identity(entry, dropbox_path)
            listing_id = resolve_listing_id(dropbox_path)
            if listing_id:
                candidates.append((dropbox_path, listing_id, identity))

        queued = 0
        studio_id = str(STUDIO_ID)
        with db.tx(immediate=True) as con:
            for dropbox_path, listing_id, identity in candidates:
                legacy = con.execute(
                    """SELECT id FROM dropbox_ingest_log
                       WHERE studio_id=? AND dropbox_path_lower=?
                         AND provider_file_id='' AND status='done'
                       ORDER BY id LIMIT 1""",
                    (studio_id, identity["path_lower"]),
                ).fetchone()
                if legacy:
                    continue
                cur = con.execute(
                    """INSERT INTO dropbox_ingest_log
                       (studio_id, dropbox_path, dropbox_path_lower, provider_file_id,
                        provider_revision, provider_content_hash, provider_key,
                        listing_id, status)
                       VALUES (?,?,?,?,?,?,?,?, 'queued')
                       ON CONFLICT(studio_id, provider_key) DO NOTHING""",
                    (
                        studio_id,
                        dropbox_path,
                        identity["path_lower"],
                        identity["file_id"],
                        identity["revision"],
                        identity["content_hash"],
                        identity["provider_key"],
                        listing_id,
                    ),
                )
                if cur.rowcount != 1:
                    continue
                log_id = cur.lastrowid
                job_id = jobs.enqueue(
                    "dropbox_ingest",
                    {
                        "studio_id": studio_id,
                        "log_id": log_id,
                        "dropbox_path": dropbox_path,
                        "listing_id": listing_id,
                    },
                    idempotency_key=f"dropbox-ingest:{identity['provider_key']}",
                )
                con.execute(
                    """UPDATE dropbox_ingest_log SET job_id=?, updated_at=datetime('now')
                       WHERE id=? AND studio_id=? AND status='queued'""",
                    (job_id, log_id, studio_id),
                )
                queued += 1
            new_cursor = data.get("cursor")
            if new_cursor:
                con.execute(
                    """INSERT INTO dropbox_sync_state (studio_id, cursor, last_scan_at)
                       VALUES (?,?,datetime('now'))
                       ON CONFLICT(studio_id) DO UPDATE SET
                         cursor=excluded.cursor, last_scan_at=excluded.last_scan_at""",
                    (studio_id, new_cursor),
                )
            integration_events.set_sync_status("dropbox", ok=True)
            if queued:
                integration_events.log_event("dropbox", "scan.queued", detail=f"{queued} files")
        return queued
    except Exception as e:
        log.exception("dropbox scan failed studio=%s", STUDIO_ID)
        _record_scan_failure(e)
        raise


def _claim_ingest(log_id: int, dropbox_path: str, listing_id: int) -> tuple[dict, str] | None:
    studio_id = str(STUDIO_ID)
    claim_token = uuid.uuid4().hex
    with db.tx(immediate=True) as con:
        row = con.execute(
            """SELECT * FROM dropbox_ingest_log
               WHERE id=? AND studio_id=? AND dropbox_path=? AND listing_id=?""",
            (log_id, studio_id, dropbox_path, listing_id),
        ).fetchone()
        if not row:
            raise RuntimeError("dropbox ingest log not found for this studio")
        if row["status"] == "done":
            return None
        cur = con.execute(
            """UPDATE dropbox_ingest_log
               SET status='ingesting', attempts=attempts+1, claimed_at=datetime('now'),
                   claim_token=?, error=NULL, updated_at=datetime('now')
               WHERE id=? AND studio_id=? AND (
                 status IN ('queued','failed') OR
                 (status='ingesting' AND claimed_at < datetime('now','-15 minutes'))
               )""",
            (claim_token, log_id, studio_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("dropbox ingest is already in progress")
        claimed = con.execute(
            "SELECT * FROM dropbox_ingest_log WHERE id=? AND studio_id=?",
            (log_id, studio_id),
        ).fetchone()
        return dict(claimed), claim_token


def _download_metadata(resp) -> dict:
    raw = resp.headers.get("Dropbox-API-Result") or resp.headers.get("dropbox-api-result")
    if not raw:
        raise RuntimeError("Dropbox download omitted revision metadata")
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as e:
        raise RuntimeError("Dropbox download returned invalid revision metadata") from e


def _verify_download(log_row: dict, resp) -> None:
    if not log_row["provider_file_id"] or not log_row["provider_revision"]:
        return
    metadata = _download_metadata(resp)
    if metadata.get("id") != log_row["provider_file_id"]:
        raise RuntimeError("Dropbox download file identity changed")
    if metadata.get("rev") != log_row["provider_revision"]:
        raise RuntimeError("Dropbox download revision changed")
    expected_hash = log_row["provider_content_hash"]
    if expected_hash and metadata.get("content_hash") != expected_hash:
        raise RuntimeError("Dropbox download content hash changed")


def _mark_ingest_failed(log_id: int, claim_token: str, error: Exception) -> None:
    with db.tx(immediate=True) as con:
        con.execute(
            """UPDATE dropbox_ingest_log
               SET status='failed', error=?, claimed_at=NULL, claim_token=NULL,
                   updated_at=datetime('now')
               WHERE id=? AND studio_id=? AND status='ingesting' AND claim_token=?""",
            (str(error)[:500], log_id, str(STUDIO_ID), claim_token),
        )


def _finalize_ingest(
    *,
    log_row: dict,
    claim_token: str,
    gallery_id: int,
    filename: str,
    stored: str,
    size: int,
) -> int:
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        current = con.execute(
            """SELECT status, claim_token FROM dropbox_ingest_log
               WHERE id=? AND studio_id=?""",
            (log_row["id"], studio_id),
        ).fetchone()
        if not current or current["status"] != "ingesting" or current["claim_token"] != claim_token:
            raise RuntimeError("dropbox ingest claim was lost")
        section = con.execute(
            "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1",
            (gallery_id,),
        ).fetchone()
        asset_id = con.execute(
            """INSERT INTO assets (gallery_id, section_id, kind, filename, stored, bytes)
               VALUES (?,?,?,?,?,?)""",
            (gallery_id, section["id"] if section else None, "photo", filename, stored, size),
        ).lastrowid
        cur = con.execute(
            """UPDATE dropbox_ingest_log
               SET status='done', asset_id=?, stored=?, error=NULL,
                   claimed_at=NULL, claim_token=NULL, updated_at=datetime('now')
               WHERE id=? AND studio_id=? AND status='ingesting' AND claim_token=?""",
            (asset_id, stored, log_row["id"], studio_id, claim_token),
        )
        if cur.rowcount != 1:
            raise RuntimeError("dropbox ingest finalize CAS was lost")
        jobs.enqueue(
            "image_derivatives",
            {"asset_id": asset_id},
            idempotency_key=f"dropbox-derivatives:{log_row['provider_key']}",
        )
        con.execute(
            "UPDATE galleries SET content_rev=content_rev+1 WHERE id=? AND studio_id=?",
            (gallery_id, studio_id),
        )
        db.audit(
            "integration",
            "dropbox.ingest",
            f"path={log_row['dropbox_path']} asset={asset_id}",
        )
        return asset_id


def ingest_file(*, log_id: int, dropbox_path: str, listing_id: int) -> None:
    claimed = _claim_ingest(log_id, dropbox_path, listing_id)
    if not claimed:
        return
    log_row, claim_token = claimed
    token = _token()
    if not token:
        error = RuntimeError("dropbox not connected")
        _mark_ingest_failed(log_id, claim_token, error)
        raise error
    from .. import media_paths

    gallery_id = _gallery_for_listing(listing_id)
    for sub in ("original", "web", "thumb"):
        media_paths.gallery_subdir(gallery_id, sub)
    filename = Path(dropbox_path).name
    ext = Path(filename).suffix.lower()
    if ext not in PHOTO_EXTS:
        error = RuntimeError(f"unsupported type: {ext}")
        _mark_ingest_failed(log_id, claim_token, error)
        raise error
    stored = f"dbx-{log_row['provider_key']}{ext}"
    dest = media_paths.gallery_subdir(gallery_id, "original") / stored
    temp = dest.with_name(f".{dest.name}.{claim_token}.part")
    # Bind the download to the immutable revision, not a mutable human path.
    provider_path = (
        f"rev:{log_row['provider_revision']}"
        if log_row["provider_revision"]
        else log_row["provider_file_id"] or dropbox_path
    )
    api_arg = {"path": provider_path}
    headers = {
        "Authorization": f"Bearer {token}",
        "Dropbox-API-Arg": json.dumps(api_arg, sort_keys=True, separators=(",", ":")),
    }
    try:
        resp = httpx.post(
            "https://content.dropboxapi.com/2/files/download",
            headers=headers,
            timeout=120,
        )
        resp.raise_for_status()
        _verify_download(log_row, resp)
        temp.write_bytes(resp.content)
        temp.replace(dest)
        _finalize_ingest(
            log_row=log_row,
            claim_token=claim_token,
            gallery_id=gallery_id,
            filename=filename,
            stored=stored,
            size=dest.stat().st_size,
        )
    except Exception as e:
        _mark_ingest_failed(log_id, claim_token, e)
        raise
    finally:
        temp.unlink(missing_ok=True)


def retry_failed(log_id: int) -> None:
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        row = con.execute(
            """SELECT * FROM dropbox_ingest_log
               WHERE id=? AND studio_id=? AND status='failed'""",
            (log_id, studio_id),
        ).fetchone()
        if not row:
            raise ValueError("log entry not found or not failed")
        con.execute(
            """UPDATE dropbox_ingest_log
               SET status='queued', error=NULL, updated_at=datetime('now')
               WHERE id=? AND studio_id=? AND status='failed'""",
            (log_id, studio_id),
        )
        if row["job_id"]:
            job = con.execute(
                "SELECT status FROM jobs WHERE id=? AND studio_id=?",
                (row["job_id"], studio_id),
            ).fetchone()
            if job and job["status"] == "failed":
                if not jobs.retry_job(row["job_id"]):
                    raise RuntimeError("dropbox ingest job retry was lost")
                return
            if job and job["status"] in {"queued", "running"}:
                return
            raise RuntimeError("dropbox ingest job is not retryable")
        job_id = jobs.enqueue(
            "dropbox_ingest",
            {
                "studio_id": studio_id,
                "log_id": log_id,
                "dropbox_path": row["dropbox_path"],
                "listing_id": row["listing_id"],
            },
            idempotency_key=f"dropbox-ingest:{row['provider_key']}",
        )
        con.execute(
            "UPDATE dropbox_ingest_log SET job_id=? WHERE id=? AND studio_id=?",
            (job_id, log_id, studio_id),
        )


def scan_now() -> int:
    return scan_folder()


def sweep_all() -> int:
    total = 0
    rows = db.all_(
        """SELECT sp.studio_id FROM studio_profiles sp
           JOIN studio_oauth o ON o.studio_id=sp.studio_id AND o.provider=?
           WHERE sp.dropbox_enabled=1""",
        (PROVIDER,),
    )
    from .. import tenant

    for row in rows:
        tenant.set_studio(row["studio_id"])
        total += scan_folder()
    return total
