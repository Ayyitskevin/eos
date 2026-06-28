"""Request-scoped tenant context — subdomain + session resolution."""

from __future__ import annotations

import contextvars
import logging

from fastapi import Request

from . import config, db, security

log = logging.getLogger("eos.tenant")

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
        scheme = "https" if config.COOKIE_SECURE else "http"
        _base_url.set(f"{scheme}://{studio_row['custom_domain']}")
    elif config.BASE_DOMAIN and row["slug"] and studio_id != "default":
        scheme = "https" if config.COOKIE_SECURE else "http"
        port = ""
        if ":" in config.BASE_URL and not config.COOKIE_SECURE:
            host_part = config.BASE_URL.split("://", 1)[-1]
            if ":" in host_part:
                port = ":" + host_part.split(":", 1)[1]
        _base_url.set(f"{scheme}://{row['slug']}.{config.BASE_DOMAIN}{port}")
    else:
        _base_url.set(config.BASE_URL)


def set_studio(studio_id: str) -> None:
    _studio_id.set(studio_id)
    _refresh_branding(studio_id)


def subdomain_from_host(host: str | None) -> str | None:
    if not host or not config.BASE_DOMAIN:
        return None
    host = host.split(":")[0].lower()
    base = config.BASE_DOMAIN.lower()
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


def studio_id_for_custom_domain(host: str | None) -> str | None:
    if not host:
        return None
    host = host.split(":")[0].lower()
    row = db.one(
        """SELECT id FROM studio
           WHERE lower(custom_domain)=? AND custom_domain_verified=1 AND active=1""",
        (host,),
    )
    return row["id"] if row else None


def resolve_tenant(request: Request) -> str:
    from . import platform_admin

    imp = platform_admin.impersonated_studio_id(request)
    if imp:
        return imp
    if request.url.path.startswith("/demo"):
        from . import config, demo_sandbox

        if config.DEMO_ENABLED:
            return demo_sandbox.DEMO_STUDIO_ID
    host = request.headers.get("host")
    custom = studio_id_for_custom_domain(host)
    if custom:
        return custom
    sub = subdomain_from_host(host)
    if sub:
        sid = studio_id_for_slug(sub)
        if sid:
            return sid
    if host and host.split(":")[0].lower() == "demo" and not config.BASE_DOMAIN:
        from . import demo_sandbox

        if config.DEMO_ENABLED:
            return demo_sandbox.DEMO_STUDIO_ID
    uid = security.current_user_id(request)
    if uid:
        row = db.one("SELECT studio_id FROM users WHERE id=? AND active=1", (uid,))
        if row:
            return row["studio_id"]
    return "default"


def bind_request(request: Request) -> None:
    set_studio(resolve_tenant(request))
