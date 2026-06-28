"""Studio signup and tenant provisioning."""

import re

from fastapi import HTTPException

from . import db, security, studio_seed, users
from . import config

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_RESERVED = {"www", "admin", "api", "app", "mail", "default", "signup", "static"}


def _normalize_slug(raw: str) -> str:
    slug = raw.strip().lower().replace(" ", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def create_studio(
    *,
    name: str,
    slug: str,
    owner_email: str,
    owner_password: str,
    owner_name: str = "",
    timezone: str = "America/New_York",
) -> dict:
    name = name.strip()
    slug = _normalize_slug(slug)
    email = owner_email.strip().lower()
    if not name or not slug or not email or not owner_password:
        raise HTTPException(status_code=400, detail="All fields are required.")
    if not _SLUG_RE.match(slug) or slug in _RESERVED:
        raise HTTPException(status_code=400, detail="Invalid studio URL slug.")
    if db.one("SELECT 1 AS x FROM studio WHERE slug=?", (slug,)):
        raise HTTPException(status_code=409, detail="That studio URL is already taken.")
    if db.one("SELECT 1 AS x FROM users WHERE lower(email)=?", (email,)):
        raise HTTPException(status_code=409, detail="Email already registered.")

    studio_id = slug
    db.run(
        """INSERT INTO studio (id, name, slug, contact_email, timezone, saas_enabled, plan_tier)
           VALUES (?,?,?,?,?,1,'trial')""",
        (studio_id, name, slug, email, timezone),
    )
    studio_seed.seed_studio(studio_id)
    uid = users.create_user(
        email, owner_password, name=owner_name or "Owner", role="owner", studio_id=studio_id,
    )
    from . import platform_billing
    platform_billing.provision_new_studio(studio_id, email=email, name=name)
    db.audit("signup", "studio.create", f"id={studio_id} owner={email}")
    login_url = f"{config.BASE_URL}/admin/login"
    if config.BASE_DOMAIN:
        scheme = "https" if config.COOKIE_SECURE else "http"
        port = ""
        if ":" in config.BASE_URL and not config.COOKIE_SECURE:
            host_part = config.BASE_URL.split("://", 1)[-1]
            if ":" in host_part:
                port = ":" + host_part.split(":", 1)[1]
        login_url = f"{scheme}://{slug}.{config.BASE_DOMAIN}{port}/admin/login"
    return {
        "studio_id": studio_id,
        "slug": slug,
        "owner_id": uid,
        "login_url": login_url,
    }