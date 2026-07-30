"""Cookies, PIN lockout, slugs, client IP resolution."""

import hashlib
import hmac
import ipaddress
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
    return f"{secrets.randbelow(1000000):06d}"


def new_token() -> str:
    return secrets.token_urlsafe(24)


def sign(value: str) -> str:
    return _serializer().dumps(value)


def unsign(token: str) -> str | None:
    try:
        return _serializer().loads(token, max_age=config.SESSION_MAX_AGE)
    except BadSignature:
        return None


CLIENT_IP_HEADER = "x-eos-client-ip"


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        parsed = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        return parsed.ipv4_mapped
    return parsed


def _is_trusted_proxy_peer(peer: str) -> bool:
    parsed = _parse_ip(peer)
    return parsed is not None and parsed.is_loopback


def client_ip(request: Request) -> str:
    """Return the socket peer, or the proxy-overwritten client IP from loopback."""
    peer = request.client.host if request.client else "?"
    parsed_peer = _parse_ip(peer)
    if _is_trusted_proxy_peer(peer):
        forwarded = _parse_ip(request.headers.get(CLIENT_IP_HEADER, ""))
        if forwarded is not None:
            return str(forwarded)
    return str(parsed_peer) if parsed_peer is not None else peer


def pin_locked(ip: str, gallery_id: int) -> bool:
    cutoff = time.time() - config.PIN_LOCKOUT_MIN * 60
    row = db.one(
        "SELECT COUNT(*) AS n FROM pin_attempts WHERE ip=? AND gallery_id=? AND ts>?",
        (ip, gallery_id, cutoff),
    )
    return row["n"] >= config.PIN_MAX_FAILS


def gallery_pin_locked(gallery_id: int) -> bool:
    """IP-independent lockout: distributed guessing trips a per-gallery counter."""
    cutoff = time.time() - config.PIN_LOCKOUT_MIN * 60
    row = db.one(
        "SELECT COUNT(*) AS n FROM pin_attempts WHERE gallery_id=? AND ts>?",
        (gallery_id, cutoff),
    )
    return row["n"] >= config.GALLERY_PIN_MAX_FAILS


def pin_fail(ip: str, gallery_id: int) -> None:
    db.run(
        "INSERT INTO pin_attempts (ip, gallery_id, ts) VALUES (?,?,?)",
        (ip, gallery_id, time.time()),
    )
    db.run("DELETE FROM pin_attempts WHERE ts < ?", (time.time() - 86400,))
    log.warning("bad PIN for gallery %s from %s", gallery_id, ip)


def pin_clear(ip: str, gallery_id: int) -> None:
    db.run("DELETE FROM pin_attempts WHERE ip=? AND gallery_id=?", (ip, gallery_id))


def gallery_pin_clear(gallery_id: int) -> None:
    """A correct PIN proves the legitimate client; reset all counters for the gallery."""
    db.run("DELETE FROM pin_attempts WHERE gallery_id=?", (gallery_id,))


GALLERY_COOKIE_PREFIX = "eos_g"


def gallery_cookie_name(gallery_id: int) -> str:
    return f"{GALLERY_COOKIE_PREFIX}{gallery_id}"


def _gallery_access_claim(gallery) -> str:
    payload = f"{gallery['id']}:{gallery['pin']}:{gallery['delivery_token']}".encode()
    return hmac.new(config.SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


def gallery_unlocked(request: Request, gallery) -> bool:
    raw = request.cookies.get(gallery_cookie_name(gallery["id"]))
    claim = unsign(raw) if raw else None
    expected = _gallery_access_claim(gallery)
    return bool(claim) and secrets.compare_digest(claim, expected)


def set_gallery_cookie(gallery) -> tuple[str, str]:
    return gallery_cookie_name(gallery["id"]), sign(_gallery_access_claim(gallery))


ADMIN_COOKIE = "eos_admin"
_SESSION_PREFIX = "sess:"
_SESSION_CACHE_MISS = object()


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _create_session(user_id: int | None, *, ip: str = "") -> str:
    from . import tenant

    now = time.time()
    db.run(
        "DELETE FROM admin_sessions WHERE expires_at<? OR revoked_at<?",
        (now, now - 86400),
    )
    token = secrets.token_urlsafe(32)
    db.run(
        """INSERT INTO admin_sessions (token_hash, user_id, studio_id, ip, expires_at)
           VALUES (?,?,?,?,?)""",
        (
            _hash_session_token(token),
            user_id,
            tenant.get_studio_id(),
            ip,
            now + config.SESSION_MAX_AGE,
        ),
    )
    return token


def _session_token(request: Request) -> str | None:
    raw = request.cookies.get(ADMIN_COOKIE)
    if not raw:
        return None
    val = unsign(raw)
    if not val or not val.startswith(_SESSION_PREFIX):
        return None
    return val[len(_SESSION_PREFIX) :]


def _lookup_session_row(request: Request):
    token = _session_token(request)
    if not token:
        return None
    row = db.one(
        "SELECT * FROM admin_sessions WHERE token_hash=?",
        (_hash_session_token(token),),
    )
    if not row or row["revoked_at"] is not None or row["expires_at"] < time.time():
        return None
    return row


def _session_row(request: Request):
    cached = getattr(request.state, "eos_admin_session", _SESSION_CACHE_MISS)
    if cached is _SESSION_CACHE_MISS:
        cached = _lookup_session_row(request)
        request.state.eos_admin_session = cached
    return cached


def is_admin(request: Request) -> bool:
    return _session_row(request) is not None


def current_user_id(request: Request) -> int | None:
    row = _session_row(request)
    return row["user_id"] if row else None


def set_session_cookie(user_id: int | None = None, *, ip: str = "") -> tuple[str, str]:
    return ADMIN_COOKIE, sign(f"{_SESSION_PREFIX}{_create_session(user_id, ip=ip)}")


def revoke_session(request: Request) -> None:
    token = _session_token(request)
    if not token:
        return
    db.run(
        "UPDATE admin_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
        (time.time(), _hash_session_token(token)),
    )


def revoke_user_sessions(user_id: int) -> None:
    db.run(
        "UPDATE admin_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
        (time.time(), user_id),
    )


def legacy_admin_allowed() -> bool:
    from . import config, db

    if config.SAAS_MODE or config.SIGNUP_ENABLED or config.COOKIE_SECURE:
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
    if not site and not request.cookies.get(ADMIN_COOKIE):
        # No browser metadata and no authenticated session at stake — nothing to forge.
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
LOGIN_EMAIL_BUCKET = -6
API_TOKEN_BUCKET = -7


def email_login_locked(email: str) -> bool:
    """Per-account lockout: IP rotation must not reset login throttling."""
    cutoff = time.time() - config.PIN_LOCKOUT_MIN * 60
    row = db.one(
        "SELECT COUNT(*) AS n FROM pin_attempts WHERE ip=? AND gallery_id=? AND ts>?",
        (email, LOGIN_EMAIL_BUCKET, cutoff),
    )
    return row["n"] >= config.LOGIN_EMAIL_MAX_FAILS


def email_login_fail(email: str) -> None:
    db.run(
        "INSERT INTO pin_attempts (ip, gallery_id, ts) VALUES (?,?,?)",
        (email, LOGIN_EMAIL_BUCKET, time.time()),
    )


def email_login_clear(email: str) -> None:
    db.run(
        "DELETE FROM pin_attempts WHERE ip=? AND gallery_id=?",
        (email, LOGIN_EMAIL_BUCKET),
    )


def api_token_locked(ip: str) -> bool:
    cutoff = time.time() - config.PIN_LOCKOUT_MIN * 60
    row = db.one(
        "SELECT COUNT(*) AS n FROM pin_attempts WHERE ip=? AND gallery_id=? AND ts>?",
        (ip, API_TOKEN_BUCKET, cutoff),
    )
    return row["n"] >= config.API_TOKEN_MAX_FAILS


def api_token_fail(ip: str) -> None:
    db.run(
        "INSERT INTO pin_attempts (ip, gallery_id, ts) VALUES (?,?,?)",
        (ip, API_TOKEN_BUCKET, time.time()),
    )


def api_token_clear(ip: str) -> None:
    db.run(
        "DELETE FROM pin_attempts WHERE ip=? AND gallery_id=?",
        (ip, API_TOKEN_BUCKET),
    )


def claim_signup_attempt(ip: str) -> bool:
    """Atomically consume one signup slot, or reject without recording."""
    now = time.time()
    cutoff = now - config.SIGNUP_RATE_WINDOW_SEC
    with db.tx(immediate=True) as con:
        con.execute(
            "DELETE FROM pin_attempts WHERE gallery_id=? AND ts<=?",
            (SIGNUP_BUCKET, cutoff),
        )
        row = con.execute(
            """SELECT COUNT(*) AS n FROM pin_attempts
               WHERE ip=? AND gallery_id=? AND ts>?""",
            (ip, SIGNUP_BUCKET, cutoff),
        ).fetchone()
        if row["n"] >= config.SIGNUP_RATE_LIMIT:
            return False
        con.execute(
            "INSERT INTO pin_attempts (ip, gallery_id, ts) VALUES (?,?,?)",
            (ip, SIGNUP_BUCKET, now),
        )
    return True


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


# --- In-process rate limiting -------------------------------------------
# Eos runs as a single application worker by design (docs/SCALE.md), so
# process-local sliding-window buckets are correct shared state. If the app
# ever scales to multiple workers, move these buckets to a shared store
# (Redis/DB) instead.

RATE_WINDOW_SEC = 60
_RATE_EVICT_THRESHOLD = 10000
_rate_hits: dict[str, list[float]] = {}


def rate_limit_retry_after(key: str, limit_per_min: int) -> int:
    """Consume one slot for key; return Retry-After seconds when over the limit.

    A return value of 0 means the request is allowed. A non-positive limit
    disables limiting entirely.
    """
    if limit_per_min <= 0:
        return 0
    now = time.monotonic()
    cutoff = now - RATE_WINDOW_SEC
    hits = [t for t in _rate_hits.get(key, []) if t > cutoff]
    if len(hits) >= limit_per_min:
        _rate_hits[key] = hits
        return max(1, int(RATE_WINDOW_SEC - (now - hits[0])) + 1)
    hits.append(now)
    _rate_hits[key] = hits
    if len(_rate_hits) > _RATE_EVICT_THRESHOLD:
        stale = [k for k, v in _rate_hits.items() if not v or v[-1] <= cutoff]
        for k in stale:
            del _rate_hits[k]
    return 0


def check_rate_limit(key: str, limit_per_min: int, detail: str = "rate limit exceeded") -> None:
    """Raise HTTP 429 with Retry-After when the key is over its per-minute limit."""
    retry = rate_limit_retry_after(key, limit_per_min)
    if retry:
        log.warning("rate limit hit for %s (retry after %ss)", key, retry)
        raise HTTPException(
            status_code=429,
            detail=detail,
            headers={"Retry-After": str(retry)},
        )


def reset_rate_limits() -> None:
    """Test hook: drop all in-process rate-limit state."""
    _rate_hits.clear()
