"""Google OAuth callback for operator login (no /admin prefix)."""

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from .. import admin_oauth, config, security, tenant

router = APIRouter()


@router.get("/oauth/google/admin/callback")
async def google_admin_callback(code: str = "", state: str = ""):
    if not code or not state:
        return RedirectResponse("/admin/login?oauth_error=google", status_code=303)
    result = admin_oauth.handle_callback(code, state)
    if not result:
        return RedirectResponse("/admin/login?oauth_error=google", status_code=303)
    tenant.set_studio(result["studio_id"])
    resp = RedirectResponse("/admin", status_code=303)
    name, value = security.set_session_cookie(result["user_id"])
    resp.set_cookie(
        name,
        value,
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    security.set_csrf_cookie(resp)
    return resp
