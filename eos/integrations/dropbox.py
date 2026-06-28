"""Dropbox watch-folder ingest — auto-import photos into listing galleries."""

from __future__ import annotations

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
    return bool(config.DROPBOX_APP_KEY and config.DROPBOX_APP_SECRET and config.DROPBOX_REDIRECT_URI)


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
    rel = dropbox_path[len(watch):].lstrip("/")
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
    row = db.one(
        "SELECT id FROM galleries WHERE listing_id=? AND studio_id=? ORDER BY id LIMIT 1",
        (listing_id, STUDIO_ID),
    )
    if row:
        return row["id"]
    from .. import galleries
    listing = db.one("SELECT title FROM listings WHERE id=?", (listing_id,))
    return galleries.create_gallery(listing["title"], listing_id=listing_id)


def _already_ingested(dropbox_path: str) -> bool:
    row = db.one(
        "SELECT id FROM dropbox_ingest_log WHERE studio_id=? AND dropbox_path=? AND status='done'",
        (STUDIO_ID, dropbox_path),
    )
    return row is not None


def scan_folder() -> int:
    if not is_enabled():
        return 0
    token = _token()
    if not token:
        return 0
    watch = _watch_path()
    state = db.one("SELECT cursor FROM dropbox_sync_state WHERE studio_id=?", (STUDIO_ID,))
    cursor = state["cursor"] if state else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    queued = 0
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
        for entry in data.get("entries", []):
            if entry.get(".tag") != "file":
                continue
            path = entry.get("path_display") or entry.get("path_lower", "")
            if not _PHOTO_RE.search(path):
                continue
            if _already_ingested(path):
                continue
            listing_id = resolve_listing_id(path)
            if not listing_id:
                continue
            log_id = db.run(
                """INSERT INTO dropbox_ingest_log (studio_id, dropbox_path, listing_id, status)
                   VALUES (?,?,?,'queued')""",
                (STUDIO_ID, path, listing_id),
            )
            jobs.enqueue(
                "dropbox_ingest",
                {"studio_id": str(STUDIO_ID), "log_id": log_id, "dropbox_path": path, "listing_id": listing_id},
            )
            queued += 1
        new_cursor = data.get("cursor")
        if new_cursor:
            if state:
                db.run(
                    "UPDATE dropbox_sync_state SET cursor=?, last_scan_at=datetime('now') WHERE studio_id=?",
                    (new_cursor, STUDIO_ID),
                )
            else:
                db.run(
                    "INSERT INTO dropbox_sync_state (studio_id, cursor, last_scan_at) VALUES (?,?,datetime('now'))",
                    (STUDIO_ID, new_cursor),
                )
        integration_events.set_sync_status("dropbox", ok=True)
        if queued:
            integration_events.log_event("dropbox", "scan.queued", detail=f"{queued} files")
    except Exception as e:
        log.exception("dropbox scan failed studio=%s", STUDIO_ID)
        integration_events.set_sync_status("dropbox", ok=False, error=str(e))
        integration_events.log_event("dropbox", "scan.failed", detail=str(e), ok=False)
    return queued


def ingest_file(*, log_id: int, dropbox_path: str, listing_id: int) -> None:
    token = _token()
    if not token:
        raise RuntimeError("dropbox not connected")
    gallery_id = _gallery_for_listing(listing_id)
    base = config.MEDIA_DIR / str(gallery_id)
    for sub in ("original", "web", "thumb"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    filename = Path(dropbox_path).name
    ext = Path(filename).suffix.lower()
    if ext not in PHOTO_EXTS:
        raise RuntimeError(f"unsupported type: {ext}")
    stored = f"{uuid.uuid4().hex}{ext}"
    dest = base / "original" / stored
    headers = {
        "Authorization": f"Bearer {token}",
        "Dropbox-API-Arg": json.dumps({"path": dropbox_path}),
    }
    resp = httpx.post(
        "https://content.dropboxapi.com/2/files/download",
        headers=headers,
        timeout=120,
    )
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    size = dest.stat().st_size
    section = db.one(
        "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1",
        (gallery_id,),
    )
    asset_id = db.run(
        """INSERT INTO assets (gallery_id, section_id, kind, filename, stored, bytes)
           VALUES (?,?,?,?,?,?)""",
        (gallery_id, section["id"] if section else None, "photo", filename, stored, size),
    )
    jobs.enqueue("image_derivatives", {"asset_id": asset_id})
    db.run("UPDATE galleries SET content_rev=content_rev+1 WHERE id=?", (gallery_id,))
    db.run(
        """UPDATE dropbox_ingest_log SET status='done', asset_id=?, error=NULL WHERE id=?""",
        (asset_id, log_id),
    )
    db.audit("integration", "dropbox.ingest", f"path={dropbox_path} asset={asset_id}")


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