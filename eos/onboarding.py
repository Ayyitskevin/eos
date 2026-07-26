"""Studio signup and tenant provisioning."""

import logging
import re

from fastapi import HTTPException

from . import config, db, studio_seed, users

log = logging.getLogger("eos.onboarding")
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
    invite_code: str = "",
) -> dict:
    name = name.strip()
    slug = _normalize_slug(slug)
    email = owner_email.strip().lower()
    from . import invites

    invites.validate(invite_code)
    if not name or not slug or not email or not owner_password:
        raise HTTPException(status_code=400, detail="All fields are required.")
    if len(owner_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if not _SLUG_RE.match(slug) or slug in _RESERVED:
        raise HTTPException(status_code=400, detail="Invalid studio URL slug.")
    if db.one("SELECT 1 AS x FROM studio WHERE slug=?", (slug,)):
        raise HTTPException(status_code=409, detail="That studio URL is already taken.")
    if db.one("SELECT 1 AS x FROM users WHERE lower(email)=?", (email,)):
        raise HTTPException(status_code=409, detail="Email already registered.")

    studio_id = slug
    from . import platform_billing, security, signup_verify, tenant

    previous_studio = tenant.get_studio_id()
    try:
        with db.tx(immediate=True):
            if db.one("SELECT 1 AS x FROM studio WHERE slug=?", (slug,)):
                raise HTTPException(status_code=409, detail="That studio URL is already taken.")
            if db.one("SELECT 1 AS x FROM users WHERE lower(email)=?", (email,)):
                raise HTTPException(status_code=409, detail="Email already registered.")
            db.run(
                """INSERT INTO studio
                   (id, name, slug, contact_email, timezone, saas_enabled, plan_tier,
                    provisioning_status)
                   VALUES (?,?,?,?,?,1,'trial','provisioning')""",
                (studio_id, name, slug, email, timezone),
            )
            tenant.set_studio(studio_id)
            studio_seed.seed_studio(studio_id)
            db.run(
                "UPDATE studio_profiles SET booking_enabled=0 WHERE studio_id=?",
                (studio_id,),
            )
            uid = users.create_user(
                email,
                owner_password,
                name=owner_name or "Owner",
                role="owner",
                studio_id=studio_id,
            )
            platform_billing.start_trial()
            if config.SIGNUP_ENABLED:
                db.run(
                    """UPDATE studio SET signup_verified=0, signup_verify_token=?,
                       signup_verify_issued_at=datetime('now') WHERE id=?""",
                    (security.new_token(), studio_id),
                )
            invites.redeem(invite_code)
            db.audit("signup", "studio.create", f"id={studio_id} owner={email}")
    finally:
        tenant.set_studio(previous_studio)

    errors: list[str] = []
    tenant.set_studio(studio_id)
    try:
        try:
            if platform_billing.is_configured():
                platform_billing.ensure_customer(email=email, name=name)
        except Exception as exc:
            log.exception("Stripe customer provisioning failed for %s", studio_id)
            errors.append(f"billing: {exc}")
        try:
            if config.SIGNUP_ENABLED:
                signup_verify.issue_token(studio_id, email=email)
        except Exception as exc:
            log.exception("Verification delivery failed for %s", studio_id)
            errors.append(f"verification: {exc}")
    finally:
        tenant.set_studio(previous_studio)

    status = "degraded" if errors else "ready"
    db.run(
        "UPDATE studio SET provisioning_status=?, provisioning_error=? WHERE id=?",
        (status, "; ".join(errors)[:500] or None, studio_id),
    )

    origin = tenant.studio_origin(slug=slug)
    login_url = f"{origin}/admin/login"
    verify_pending_url = f"{origin}/admin/verify-pending"
    return {
        "studio_id": studio_id,
        "slug": slug,
        "owner_id": uid,
        "login_url": login_url,
        "verify_pending_url": verify_pending_url,
        "provisioning_status": status,
        "provisioning_error": "; ".join(errors)[:500] or None,
    }


def retry_provisioning() -> dict:
    """Idempotently retry external signup setup after a crash or provider failure."""
    from . import platform_billing, signup_verify, tenant

    studio_id = tenant.get_studio_id()
    row = db.one("SELECT * FROM studio WHERE id=?", (studio_id,))
    if not row:
        raise HTTPException(status_code=404)
    errors: list[str] = []
    try:
        if platform_billing.is_configured():
            platform_billing.ensure_customer(email=row["contact_email"], name=row["name"])
    except Exception as exc:
        log.exception("Stripe customer recovery failed for %s", studio_id)
        errors.append(f"billing: {exc}")
    try:
        if config.SIGNUP_ENABLED and signup_verify.needs_verification(studio_id):
            signup_verify.issue_token(studio_id, email=row["contact_email"])
    except Exception as exc:
        log.exception("Verification recovery failed for %s", studio_id)
        errors.append(f"verification: {exc}")
    status = "degraded" if errors else "ready"
    error = "; ".join(errors)[:500] or None
    db.run(
        "UPDATE studio SET provisioning_status=?, provisioning_error=? WHERE id=?",
        (status, error, studio_id),
    )
    db.audit("admin", "provisioning.retry", f"status={status}")
    return {"provisioning_status": status, "provisioning_error": error}
