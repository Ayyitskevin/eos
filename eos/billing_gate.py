"""Enforce platform billing status and signup verification for SaaS tenants."""

import datetime as dt

from fastapi import Request
from fastapi.responses import RedirectResponse

from . import config, db, signup_verify, tenant

_BILLING_PATHS = {"/admin/billing", "/admin/logout"}
_BILLING_PREFIXES = ("/stripe/platform/",)
_ADMIN_PUBLIC = {"/admin/login", "/admin/logout", "/admin/verify-pending"}


def check_access(request: Request) -> RedirectResponse | None:
    path = request.url.path
    if path.startswith("/admin") and path not in _ADMIN_PUBLIC:
        sid = tenant.get_studio_id()
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
    if status == "trialing" and row["trial_ends_at"]:
        try:
            ends = dt.datetime.strptime(row["trial_ends_at"][:19], "%Y-%m-%d %H:%M:%S")
            if dt.datetime.now() > ends:
                db.run(
                    "UPDATE studio SET billing_status='past_due' WHERE id=?",
                    (sid,),
                )
                status = "past_due"
        except ValueError:
            pass
    if status in ("past_due", "canceled"):
        return RedirectResponse("/admin/billing", status_code=303)
    return None