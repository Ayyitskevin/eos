"""Cookies, PIN lockout, slugs, client IP resolution."""

import logging
import secrets
import string
import time

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config, db

log = logging.getLogger("eos.security")

_BASE62 = string.ascii_letters + string.digits
ADMIN_BUCKET = 0


def _serializer() -> URLSafeTimedSerializer:
    if not config.SECRET_KEY:
        raise RuntimeError("EOS_SECRET_KEY is not set")
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="eos")


def new_slug(n: int = 14) -> str:
    return "".join(secrets.choice(_BASE62) for _ in range(n))


def new_pin() -> str:
    return f"{secrets.randbelow(10000):04d}"


def new_token() -> str:
    return secrets.token_urlsafe(24)


def sign(value: str) -> str:
    return _serializer().dumps(value)


def unsign(token: str) -> str | None:
    try:
        return _serializer().loads(token, max_age=config.SESSION_MAX_AGE)
    except BadSignature:
        return None


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "?"
    if peer in ("127.0.0.1", "::1"):
        return request.headers.get("cf-connecting-ip", peer)
    return peer


def pin_locked(ip: str, gallery_id: int) -> bool:
    cutoff = time.time() - config.PIN_LOCKOUT_MIN * 60
    row = db.one(
        "SELECT COUNT(*) AS n FROM pin_attempts WHERE ip=? AND gallery_id=? AND ts>?",
        (ip, gallery_id, cutoff),
    )
    return row["n"] >= config.PIN_MAX_FAILS


def pin_fail(ip: str, gallery_id: int) -> None:
    db.run(
        "INSERT INTO pin_attempts (ip, gallery_id, ts) VALUES (?,?,?)",
        (ip, gallery_id, time.time()),
    )
    db.run("DELETE FROM pin_attempts WHERE ts < ?", (time.time() - 86400,))
    log.warning("bad PIN for gallery %s from %s", gallery_id, ip)


def pin_clear(ip: str, gallery_id: int) -> None:
    db.run("DELETE FROM pin_attempts WHERE ip=? AND gallery_id=?", (ip, gallery_id))


GALLERY_COOKIE_PREFIX = "eos_g"


def gallery_cookie_name(gallery_id: int) -> str:
    return f"{GALLERY_COOKIE_PREFIX}{gallery_id}"


def gallery_unlocked(request: Request, gallery_id: int) -> bool:
    raw = request.cookies.get(gallery_cookie_name(gallery_id))
    return bool(raw) and unsign(raw) == str(gallery_id)


def set_gallery_cookie(gallery_id: int) -> tuple[str, str]:
    return gallery_cookie_name(gallery_id), sign(str(gallery_id))


ADMIN_COOKIE = "eos_admin"


def is_admin(request: Request) -> bool:
    raw = request.cookies.get(ADMIN_COOKIE)
    return bool(raw) and unsign(raw) == "admin"


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


def check_admin_password(password: str) -> bool:
    if not config.ADMIN_PASSWORD:
        return False
    return secrets.compare_digest(password, config.ADMIN_PASSWORD)


INQUIRY_BUCKET_BOOK = -3
INQUIRY_WINDOW_SEC = 3600
INQUIRY_MAX_PER_WINDOW = 3


def inquiry_throttled(ip: str, bucket: int) -> bool:
    cutoff = time.time() - INQUIRY_WINDOW_SEC
    row = db.one(
        "SELECT COUNT(*) AS n FROM pin_attempts WHERE ip=? AND gallery_id=? AND ts>?",
        (ip, bucket, cutoff),
    )
    return row["n"] >= INQUIRY_MAX_PER_WINDOW


def inquiry_record(ip: str, bucket: int) -> None:
    db.run(
        "INSERT INTO pin_attempts (ip, gallery_id, ts) VALUES (?,?,?)",
        (ip, bucket, time.time()),
    )
    db.run("DELETE FROM pin_attempts WHERE ts < ?", (time.time() - max(86400, INQUIRY_WINDOW_SEC),))