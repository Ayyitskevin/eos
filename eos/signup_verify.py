"""Signup email verification before studio activation."""

import logging

from fastapi import HTTPException

from . import config, db, mailer, security, tenant

log = logging.getLogger("eos.signup_verify")


def needs_verification(studio_id: str) -> bool:
    if not config.SIGNUP_ENABLED:
        return False
    row = db.one("SELECT signup_verified FROM studio WHERE id=?", (studio_id,))
    return bool(row and not row["signup_verified"])


def issue_token(studio_id: str, *, email: str) -> str:
    token = security.new_token()
    db.run(
        "UPDATE studio SET signup_verified=0, signup_verify_token=? WHERE id=?",
        (token, studio_id),
    )
    if mailer.configured():
        url = _verify_url(studio_id, token)
        mailer.send(
            email,
            f"Verify your Eos studio — {studio_id}",
            f"Click to activate your studio:\n\n{url}\n\nThis link expires in 48 hours.",
        )
    else:
        log.warning("mailer not configured — auto-verifying studio %s", studio_id)
        mark_verified(studio_id)
    return token


def _verify_url(studio_id: str, token: str) -> str:
    row = db.one("SELECT slug FROM studio WHERE id=?", (studio_id,))
    if config.BASE_DOMAIN and row and row["slug"]:
        scheme = "https" if config.COOKIE_SECURE else "http"
        port = ""
        if ":" in config.BASE_URL and not config.COOKIE_SECURE:
            host_part = config.BASE_URL.split("://", 1)[-1]
            if ":" in host_part:
                port = ":" + host_part.split(":", 1)[1]
        base = f"{scheme}://{row['slug']}.{config.BASE_DOMAIN}{port}"
    else:
        base = config.BASE_URL
    return f"{base}/verify/{token}"


def mark_verified(studio_id: str) -> None:
    db.run(
        "UPDATE studio SET signup_verified=1, signup_verify_token=NULL WHERE id=?",
        (studio_id,),
    )


def verify_token(token: str) -> str:
    row = db.one(
        "SELECT id, slug FROM studio WHERE signup_verify_token=? AND active=1",
        (token,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Invalid or expired verification link.")
    mark_verified(row["id"])
    tenant.set_studio(row["id"])
    db.audit("signup", "studio.verified", f"id={row['id']}")
    return row["slug"]
