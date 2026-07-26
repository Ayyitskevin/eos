"""Enforce platform billing status and signup verification for SaaS tenants."""

import datetime as dt

from fastapi import Request
from fastapi.responses import RedirectResponse

from . import config, db, signup_verify, tenant

_BILLING_PATHS = {"/admin/billing", "/admin/logout"}
_BILLING_PREFIXES = ("/stripe/platform/",)
_ADMIN_PUBLIC = {
    "/admin/login",
    "/admin/logout",
    "/admin/verify-pending",
    "/admin/verify-pending/resend",
    "/admin/verify-pending/reconcile",
}


def _trial_end_utc(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        ends = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if ends.tzinfo is None:
        return ends.replace(tzinfo=dt.UTC)
    return ends.astimezone(dt.UTC)


def has_billing_access(
    billing_status: object,
    trial_ends_at: object,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """Return whether a billing state grants access, failing closed on bad trial data."""
    if billing_status == "active":
        return True
    if billing_status != "trialing":
        return False
    ends = _trial_end_utc(trial_ends_at)
    if ends is None:
        return False
    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.UTC)
    else:
        current = current.astimezone(dt.UTC)
    return ends > current


def check_access(request: Request) -> RedirectResponse | None:
    path = request.url.path
    sid = tenant.get_studio_id()
    if sid != "default":
        active_row = db.one("SELECT active FROM studio WHERE id=?", (sid,))
        if active_row and not active_row["active"]:
            from .render import templates

            if path.startswith("/admin") and path not in _ADMIN_PUBLIC:
                return RedirectResponse("/admin/login?suspended=1", status_code=303)
            return templates.TemplateResponse(
                request,
                "public/error.html",
                {"message": "Not found."},
                status_code=404,
            )
    if path.startswith("/admin") and path not in _ADMIN_PUBLIC:
        if sid != "default" and signup_verify.needs_verification(sid):
            return RedirectResponse("/admin/verify-pending", status_code=303)
    if not config.BILLING_ENFORCE:
        return None
    if path in _BILLING_PATHS or path.startswith(_BILLING_PREFIXES):
        return None
    if not path.startswith("/admin"):
        return None
    if path in _ADMIN_PUBLIC:
        return None
    sid = tenant.get_studio_id()
    if sid == "default":
        return None
    row = db.one(
        "SELECT billing_status, trial_ends_at FROM studio WHERE id=? AND active=1",
        (sid,),
    )
    if not row:
        return None
    status = row["billing_status"]
    now = dt.datetime.now(dt.UTC)
    if not has_billing_access(status, row["trial_ends_at"], now=now):
        ends = _trial_end_utc(row["trial_ends_at"])
        if status == "trialing" and ends is not None and ends <= now:
            db.run(
                "UPDATE studio SET billing_status='past_due' WHERE id=?",
                (sid,),
            )
        return RedirectResponse("/admin/billing", status_code=303)
    return None
