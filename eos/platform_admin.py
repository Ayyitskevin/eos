"""Platform super-admin — list tenants, impersonate studio context."""

import logging

from fastapi import HTTPException, Request

from . import config, db, security

log = logging.getLogger("eos.platform_admin")

IMPERSONATE_COOKIE = "eos_impersonate"


def is_platform_admin(user_id: int | None) -> bool:
    if not user_id:
        return False
    if config.PLATFORM_ADMIN_EMAILS:
        row = db.one(
            "SELECT email, is_platform_admin FROM users WHERE id=? AND active=1", (user_id,)
        )
        if not row:
            return False
        emails = {e.strip().lower() for e in config.PLATFORM_ADMIN_EMAILS.split(",") if e.strip()}
        return bool(row["is_platform_admin"]) or row["email"].lower() in emails
    row = db.one("SELECT is_platform_admin FROM users WHERE id=? AND active=1", (user_id,))
    return bool(row and row["is_platform_admin"])


def require_platform_admin(request: Request) -> None:
    security.require_admin(request)
    uid = security.current_user_id(request)
    if not is_platform_admin(uid):
        raise HTTPException(status_code=403, detail="Platform admin required")


def list_studios() -> list:
    return db.all_(
        """SELECT s.id, s.slug, s.name, s.plan_tier, s.billing_status, s.is_demo, s.created_at,
                  (SELECT COUNT(*) FROM listings l WHERE l.studio_id=s.id) AS n_listings,
                  (SELECT COUNT(*) FROM users u WHERE u.studio_id=s.id AND u.active=1) AS n_users
           FROM studio s WHERE s.active=1 ORDER BY s.created_at DESC""",
    )


def set_impersonation(response, studio_id: str) -> None:
    row = db.one("SELECT id FROM studio WHERE id=? AND active=1", (studio_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Studio not found")
    response.set_cookie(
        IMPERSONATE_COOKIE,
        security.sign(studio_id),
        max_age=3600,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def clear_impersonation(response) -> None:
    response.delete_cookie(IMPERSONATE_COOKIE, path="/")


def impersonated_studio_id(request: Request) -> str | None:
    raw = request.cookies.get(IMPERSONATE_COOKIE)
    if not raw:
        return None
    uid = security.current_user_id(request)
    if not is_platform_admin(uid):
        return None
    sid = security.unsign(raw)
    if not sid:
        return None
    row = db.one("SELECT id FROM studio WHERE id=? AND active=1", (sid,))
    return row["id"] if row else None
