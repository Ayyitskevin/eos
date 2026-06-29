"""Platform super-admin — tenant ops, impersonation, audit trail."""

import logging

from fastapi import HTTPException, Request

from . import config, db, security, usage

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


def audit(admin_user_id: int | None, action: str, *, studio_id: str = "", detail: str = "") -> None:
    db.run(
        """INSERT INTO platform_audit (admin_user_id, action, studio_id, detail)
           VALUES (?,?,?,?)""",
        (admin_user_id, action, studio_id, detail),
    )


def list_studios(*, include_inactive: bool = False) -> list:
    where = "" if include_inactive else "WHERE s.active=1"
    rows = db.all_(
        f"""SELECT s.id, s.slug, s.name, s.plan_tier, s.billing_status, s.is_demo,
                  s.active, s.created_at, s.custom_domain, s.custom_domain_verified,
                  s.stripe_connect_charges_enabled,
                  (SELECT COUNT(*) FROM listings l WHERE l.studio_id=s.id) AS n_listings,
                  (SELECT COUNT(*) FROM users u WHERE u.studio_id=s.id AND u.active=1) AS n_users
           FROM studio s {where}
           ORDER BY s.created_at DESC""",
    )
    out = []
    for row in rows:
        item = dict(row)
        item["storage_bytes"] = usage.studio_storage_bytes(studio_id=row["id"])
        item["storage_gb"] = round(item["storage_bytes"] / (1024**3), 2)
        out.append(item)
    return out


def platform_stats() -> dict:
    active = db.one("SELECT COUNT(*) AS n FROM studio WHERE active=1 AND is_demo=0")["n"]
    trialing = db.one(
        "SELECT COUNT(*) AS n FROM studio WHERE active=1 AND billing_status='trialing'"
    )["n"]
    paid = db.one(
        """SELECT COUNT(*) AS n FROM studio
           WHERE active=1 AND billing_status='active' AND plan_tier IN ('starter','pro')"""
    )["n"]
    listings = db.one("SELECT COUNT(*) AS n FROM listings")["n"]
    return {
        "active_studios": active,
        "trialing": trialing,
        "paid": paid,
        "total_listings": listings,
    }


def set_studio_active(studio_id: str, *, active: bool, admin_user_id: int | None) -> None:
    row = db.one("SELECT id, name FROM studio WHERE id=?", (studio_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Studio not found")
    db.run("UPDATE studio SET active=? WHERE id=?", (1 if active else 0, studio_id))
    audit(
        admin_user_id,
        "studio.suspend" if not active else "studio.reactivate",
        studio_id=studio_id,
        detail=row["name"],
    )


def set_plan_tier(
    studio_id: str,
    *,
    plan_tier: str,
    billing_status: str | None = None,
    admin_user_id: int | None,
) -> None:
    allowed = {"trial", "starter", "pro", "solo"}
    if plan_tier not in allowed:
        raise HTTPException(status_code=400, detail="Invalid plan tier")
    row = db.one("SELECT id FROM studio WHERE id=?", (studio_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Studio not found")
    if billing_status:
        db.run(
            "UPDATE studio SET plan_tier=?, billing_status=? WHERE id=?",
            (plan_tier, billing_status, studio_id),
        )
    else:
        db.run("UPDATE studio SET plan_tier=? WHERE id=?", (plan_tier, studio_id))
    audit(
        admin_user_id,
        "studio.plan_override",
        studio_id=studio_id,
        detail=f"tier={plan_tier} status={billing_status or '-'}",
    )


def set_impersonation(response, studio_id: str, *, admin_user_id: int | None) -> None:
    row = db.one("SELECT id, name FROM studio WHERE id=? AND active=1", (studio_id,))
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
    audit(admin_user_id, "studio.impersonate", studio_id=studio_id, detail=row["name"])


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
