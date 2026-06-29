"""Cookies, PIN lockout, slugs, client IP resolution."""

import logging
import secrets
import string
import time
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse
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


def _session_value(request: Request) -> str | None:
    raw = request.cookies.get(ADMIN_COOKIE)
    if not raw:
        return None
    return unsign(raw)


def is_admin(request: Request) -> bool:
    val = _session_value(request)
    return bool(val) and (val == "admin" or val.startswith("user:"))


def current_user_id(request: Request) -> int | None:
    val = _session_value(request)
    if val and val.startswith("user:"):
        try:
            return int(val.split(":", 1)[1])
        except ValueError:
            return None
    return None


def set_session_cookie(user_id: int | None = None) -> tuple[str, str]:
    if user_id is None:
        return ADMIN_COOKIE, sign("admin")
    return ADMIN_COOKIE, sign(f"user:{user_id}")


def legacy_admin_allowed() -> bool:
    from . import config, db

    if config.SAAS_MODE or config.SIGNUP_ENABLED:
        return False
    n = db.one("SELECT COUNT(*) AS n FROM studio WHERE active=1")
    return (n["n"] if n else 0) <= 1


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    from . import tenant

    uid = current_user_id(request)
    tid = tenant.get_studio_id()
    if uid:
        row = db.one("SELECT studio_id FROM users WHERE id=? AND active=1", (uid,))
        if not row or row["studio_id"] != tid:
            raise HTTPException(status_code=403, detail="session does not match this studio")
    elif tid != "default" or not legacy_admin_allowed():
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


CSRF_COOKIE = "eos_csrf"
CSRF_FORM = "_csrf"


def set_csrf_cookie(response, token: str | None = None) -> str:
    tok = token or new_token()
    response.set_cookie(
        CSRF_COOKIE,
        sign(tok),
        max_age=86400,
        httponly=False,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return tok


def _csrf_matches(cookie: str, submitted: str) -> bool:
    if not submitted:
        return False
    if secrets.compare_digest(cookie, submitted):
        return True
    unsigned = unsign(cookie)
    return bool(unsigned) and secrets.compare_digest(unsigned, submitted)


async def _urlencoded_form_token(request: Request) -> str:
    body = await request.body()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive
    parsed = parse_qs(body.decode("utf-8", errors="ignore"), keep_blank_values=True)
    values = parsed.get(CSRF_FORM) or []
    return values[0] if values else ""


async def validate_csrf(request: Request) -> PlainTextResponse | None:
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    path = request.url.path
    if path in ("/admin/login", "/admin/logout") or path.startswith(
        ("/stripe/", "/oauth/", "/api/")
    ):
        return None
    if not path.startswith("/admin"):
        return None
    site = request.headers.get("sec-fetch-site", "")
    if site and site not in ("same-origin", "same-site", "none"):
        return PlainTextResponse("cross-site request blocked", status_code=403)
    if not site:
        return None
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie:
        return PlainTextResponse("missing csrf token", status_code=403)
    submitted = request.headers.get("x-eos-csrf", "")
    content_type = request.headers.get("content-type", "")
    if not submitted and content_type.startswith("application/x-www-form-urlencoded"):
        submitted = await _urlencoded_form_token(request)
    if not _csrf_matches(cookie, submitted):
        return PlainTextResponse("invalid csrf token", status_code=403)
    return None


SIGNUP_BUCKET = -5


def signup_throttled(ip: str) -> bool:
    from . import config

    cutoff = time.time() - config.SIGNUP_RATE_WINDOW_SEC
    row = db.one(
        "SELECT COUNT(*) AS n FROM pin_attempts WHERE ip=? AND gallery_id=? AND ts>?",
        (ip, SIGNUP_BUCKET, cutoff),
    )
    return row["n"] >= config.SIGNUP_RATE_LIMIT


def signup_record(ip: str) -> None:
    db.run(
        "INSERT INTO pin_attempts (ip, gallery_id, ts) VALUES (?,?,?)",
        (ip, SIGNUP_BUCKET, time.time()),
    )


def check_admin_password(password: str) -> bool:
    if not config.ADMIN_PASSWORD:
        return False
    return secrets.compare_digest(password, config.ADMIN_PASSWORD)


INQUIRY_BUCKET_BOOK = -3
INQUIRY_BUCKET_SITE = -4
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
