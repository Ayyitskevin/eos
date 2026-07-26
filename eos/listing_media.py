"""Validated rich-media links and provider-scoped listing embeds."""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException

from . import db, listings
from .vocab import STUDIO_ID

log = logging.getLogger("eos.listing_media")

KINDS = ("matterport", "youtube", "vimeo", "iguide", "url")
IFRAME_KINDS = frozenset({"matterport", "youtube", "vimeo"})
_YOUTUBE_HOSTS = frozenset({"www.youtube.com", "www.youtube-nocookie.com"})
_PROVIDER_DOMAINS = (
    "youtube.com",
    "youtube-nocookie.com",
    "youtu.be",
    "vimeo.com",
    "matterport.com",
)
_TOKEN = re.compile(r"[A-Za-z0-9_-]+")


def _url_parts(raw_url: str):
    url = raw_url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL required")
    if "\\" in url or any(ord(char) < 33 for char in url):
        raise HTTPException(status_code=400, detail="invalid media URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid media URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or hostname.endswith(".")
    ):
        raise HTTPException(status_code=400, detail="media URL must use HTTPS")
    try:
        host = hostname.lower().encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise HTTPException(status_code=400, detail="invalid media host") from exc
    for domain in _PROVIDER_DOMAINS:
        if domain in host and host != domain and not host.endswith(f".{domain}"):
            raise HTTPException(status_code=400, detail="provider-spoof media host")
    return url, parsed, host


def _validate_youtube(parsed, host: str) -> None:
    parts = parsed.path.split("/")
    if (
        host not in _YOUTUBE_HOSTS
        or len(parts) != 3
        or parts[1] != "embed"
        or not _TOKEN.fullmatch(parts[2])
        or parsed.fragment
    ):
        raise HTTPException(status_code=400, detail="use a canonical YouTube embed URL")


def _validate_vimeo(parsed, host: str) -> None:
    parts = parsed.path.split("/")
    if (
        host != "player.vimeo.com"
        or len(parts) != 3
        or parts[1] != "video"
        or not parts[2].isdigit()
        or parsed.fragment
    ):
        raise HTTPException(status_code=400, detail="use a canonical Vimeo player URL")


def _validate_matterport(parsed, host: str) -> None:
    query = parse_qs(parsed.query, keep_blank_values=True)
    models = query.get("m", [])
    if (
        host != "my.matterport.com"
        or parsed.path not in ("/show", "/show/")
        or len(models) != 1
        or not _TOKEN.fullmatch(models[0])
        or parsed.fragment
    ):
        raise HTTPException(status_code=400, detail="use a canonical Matterport show URL")


def validate_url(kind: str, embed_url: str) -> str:
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="invalid media kind")
    url, parsed, host = _url_parts(embed_url)
    if kind == "youtube":
        _validate_youtube(parsed, host)
    elif kind == "vimeo":
        _validate_vimeo(parsed, host)
    elif kind == "matterport":
        _validate_matterport(parsed, host)
    return url


def list_for_listing(listing_id: int):
    listings.get_listing(listing_id)
    rows = db.all_(
        """SELECT * FROM listing_media
           WHERE listing_id=? AND studio_id=? ORDER BY position, id""",
        (listing_id, STUDIO_ID),
    )
    media = []
    for row in rows:
        item = dict(row)
        try:
            item["render_url"] = validate_url(item["kind"], item["embed_url"])
        except HTTPException:
            item["render_url"] = None
            log.warning("listing media %s has an unsafe stored URL", item["id"])
        item["iframe"] = bool(item["render_url"] and item["kind"] in IFRAME_KINDS)
        media.append(item)
    return media


def add_embed(listing_id: int, *, kind: str, label: str, embed_url: str) -> int:
    listings.get_listing(listing_id)
    url = validate_url(kind, embed_url)
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
