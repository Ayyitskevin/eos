"""Request-scoped tenant context — subdomain + session resolution."""

from __future__ import annotations

import contextvars
import ipaddress
import logging
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from . import config, db, security

log = logging.getLogger("eos.tenant")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

_studio_id: contextvars.ContextVar[str] = contextvars.ContextVar("studio_id", default="default")
_site_name: contextvars.ContextVar[str] = contextvars.ContextVar(
    "site_name", default=config.SITE_NAME
)
_base_url: contextvars.ContextVar[str] = contextvars.ContextVar("base_url", default=config.BASE_URL)


def get_studio_id() -> str:
    return _studio_id.get()


def get_site_name() -> str:
    return _site_name.get()


def get_base_url() -> str:
    return _base_url.get()


def normalize_host(value: str | None) -> str | None:
    """Return a canonical hostname, rejecting ambiguous Host header syntax."""
    raw = (value or "").strip()
    if not raw or any(ch.isspace() for ch in raw) or any(ch in raw for ch in "/@?#"):
        return None
    try:
        parsed = urlsplit(f"//{raw}")
        host = (parsed.hostname or "").rstrip(".").lower()
        _port = parsed.port  # Force validation of malformed/non-numeric ports.
    except ValueError:
        return None
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if any(not _HOST_LABEL.match(label) for label in host.split(".")):
        return None
    return host


def _configured_port() -> int | None:
    for raw in (config.BASE_DOMAIN, urlsplit(config.BASE_URL).netloc):
        if not raw:
            continue
        try:
            port = urlsplit(f"//{raw}").port
        except ValueError:
            continue
        if port:
            return port
    return None


def configured_base_host() -> str | None:
    return normalize_host(config.BASE_DOMAIN or urlsplit(config.BASE_URL).netloc)


def studio_origin(*, slug: str | None = None, custom_domain: str | None = None) -> str:
    """Build one canonical tenant origin without duplicating configured ports."""
    scheme = "https" if config.COOKIE_SECURE else "http"
    if custom_domain:
        host = normalize_host(custom_domain)
        if not host:
            raise ValueError("invalid custom domain")
        return f"{scheme}://{host}"
    base = configured_base_host()
    if slug and base and config.BASE_DOMAIN:
        port = _configured_port()
        suffix = f":{port}" if port and not config.COOKIE_SECURE else ""
        return f"{scheme}://{slug}.{base}{suffix}"
    return config.BASE_URL.rstrip("/")


def _refresh_branding(studio_id: str) -> None:
    row = db.one("SELECT name, slug FROM studio WHERE id=? AND active=1", (studio_id,))
    if not row:
        _site_name.set(config.SITE_NAME)
        _base_url.set(config.BASE_URL)
        return
    _site_name.set(row["name"])
    studio_row = db.one(
        "SELECT slug, custom_domain, custom_domain_verified FROM studio WHERE id=?",
        (studio_id,),
    )
    if studio_row and studio_row["custom_domain"] and studio_row["custom_domain_verified"]:
        _base_url.set(studio_origin(custom_domain=studio_row["custom_domain"]))
    elif config.BASE_DOMAIN and row["slug"] and studio_id != "default":
        _base_url.set(studio_origin(slug=row["slug"]))
    else:
        _base_url.set(config.BASE_URL)


def set_studio(studio_id: str) -> None:
    _studio_id.set(studio_id)
    _refresh_branding(studio_id)


def subdomain_from_host(host: str | None) -> str | None:
    if not host or not config.BASE_DOMAIN:
        return None
    host = normalize_host(host)
    base = configured_base_host()
    if not host or not base:
        return None
    if host == base or host == f"www.{base}":
        return None
    suffix = f".{base}"
    if host.endswith(suffix):
        sub = host[: -len(suffix)]
        if sub and "." not in sub:
            return sub
    return None


def studio_id_for_slug(slug: str) -> str | None:
    row = db.one("SELECT id FROM studio WHERE slug=? AND active=1", (slug,))
    return row["id"] if row else None


def studio_row_for_slug(slug: str):
    return db.one("SELECT id, active FROM studio WHERE slug=?", (slug,))


def studio_id_for_custom_domain(host: str | None) -> str | None:
    host = normalize_host(host)
    if not host:
        return None
    rows = db.all_(
        """SELECT id FROM studio
           WHERE lower(custom_domain)=? AND custom_domain_verified=1 AND active=1
           ORDER BY id LIMIT 2""",
        (host,),
    )
    return rows[0]["id"] if len(rows) == 1 else None


def resolve_tenant(request: Request) -> str:
    from . import platform_admin

    if request.url.path.startswith("/demo"):
        from . import demo_sandbox

        if config.DEMO_ENABLED:
            return demo_sandbox.DEMO_STUDIO_ID
    raw_host = request.headers.get("host")
    host = normalize_host(raw_host)
    if raw_host and not host:
        raise HTTPException(status_code=400, detail="Malformed Host header.")
    custom = studio_id_for_custom_domain(host)
    if custom:
        return custom
    sub = subdomain_from_host(host)
    if sub:
        row = studio_row_for_slug(sub)
        if row:
            return row["id"]
        raise HTTPException(status_code=404, detail="Unknown studio host.")
    if host == "demo" and not config.BASE_DOMAIN:
        from . import demo_sandbox

        if config.DEMO_ENABLED:
            return demo_sandbox.DEMO_STUDIO_ID
    if config.SAAS_MODE or config.BASE_DOMAIN:
        base_host = configured_base_host()
        platform_hosts = {
            candidate
            for candidate in (
                base_host,
                normalize_host(urlsplit(config.BASE_URL).netloc),
                f"www.{base_host}" if base_host else None,
            )
            if candidate
        }
        if host not in platform_hosts:
            raise HTTPException(status_code=404, detail="Unknown tenant host.")
        imp = platform_admin.impersonated_studio_id(request)
        if imp:
            return imp
        return "default"
    imp = platform_admin.impersonated_studio_id(request)
    if imp:
        return imp
    uid = security.current_user_id(request)
    if uid:
        row = db.one("SELECT studio_id FROM users WHERE id=? AND active=1", (uid,))
        if row:
            return row["studio_id"]
    return "default"


def public_booking_readiness(studio_id: str | None = None) -> dict:
    sid = studio_id or get_studio_id()
    row = db.one(
        """SELECT s.active, s.signup_verified, p.published, p.booking_enabled,
                  p.headline, p.service_area
           FROM studio s
           LEFT JOIN studio_profiles p ON p.studio_id=s.id
           WHERE s.id=?""",
        (sid,),
    )
    if not row:
        return {"ready": False, "reason": "studio_missing"}
    checks = {
        "active": bool(row["active"]),
        "verified": sid == "default" or bool(row["signup_verified"]),
        "published": sid == "default" or bool(row["published"]),
        "booking_enabled": bool(row["booking_enabled"]),
        "branded": sid == "default" or bool(row["headline"] and row["service_area"]),
    }
    reason = next((name for name, ok in checks.items() if not ok), "ready")
    return {"ready": all(checks.values()), "reason": reason, "checks": checks}


def bind_request(request: Request) -> None:
    set_studio(resolve_tenant(request))
