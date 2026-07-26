"""Google OAuth callback and tenant-only operator-session completion."""

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import admin_oauth, config, security, tenant
from ..render import templates

router = APIRouter()


def _private(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/oauth/google/admin/start")
async def google_admin_start(request: Request):
    try:
        start = admin_oauth.begin_login(
            studio_id=tenant.get_studio_id(),
            host=request.headers.get("host", ""),
            scheme=request.url.scheme,
        )
    except ValueError:
        return _private(RedirectResponse("/admin/login?oauth_error=google", status_code=303))
    response = RedirectResponse(start.url, status_code=303)
    response.set_cookie(
        admin_oauth.NONCE_COOKIE,
        start.browser_nonce,
        max_age=admin_oauth.NONCE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return _private(response)


@router.get("/oauth/google/admin/callback")
async def google_admin_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    if request.url.scheme != "https" or not admin_oauth.callback_host_is_apex(
        request.headers.get("host")
    ):
        return _private(RedirectResponse("/admin/login?oauth_error=google", status_code=303))
    result = admin_oauth.handle_callback(code, state, provider_error=error)
    if not result:
        return _private(RedirectResponse("/admin/login?oauth_error=google", status_code=303))
    return _private(RedirectResponse(result["redirect_url"], status_code=303))


@router.get("/oauth/google/admin/complete")
async def google_admin_complete_page(request: Request):
    studio_id = tenant.get_studio_id()
    if request.url.scheme != "https" or not admin_oauth.valid_login_host(
        studio_id, request.headers.get("host")
    ):
        return _private(JSONResponse({"detail": "Not found"}, status_code=404))
    script_nonce = secrets.token_urlsafe(24)
    response = templates.TemplateResponse(
        request,
        "admin/google_login_complete.html",
        {"script_nonce": script_nonce},
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        f"script-src 'nonce-{script_nonce}'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    return _private(response)


@router.post("/oauth/google/admin/complete")
async def google_admin_complete(request: Request):
    raw_host = request.headers.get("host", "")
    host = tenant.normalize_host(raw_host)
    expected_origin = f"https://{host}" if host else ""
    if (
        request.url.scheme != "https"
        or not host
        or request.headers.get("origin") != expected_origin
        or request.headers.get("sec-fetch-site") != "same-origin"
        or not request.headers.get("content-type", "").lower().startswith("application/json")
    ):
        return _private(JSONResponse({"detail": "Invalid login handoff"}, status_code=403))
    try:
        body = await request.json()
    except (TypeError, ValueError):
        body = {}
    token = body.get("handoff", "") if isinstance(body, dict) else ""
    if not isinstance(token, str) or len(token) > 1024:
        token = ""
    user_id = admin_oauth.consume_completion(
        token,
        browser_nonce=request.cookies.get(admin_oauth.NONCE_COOKIE, ""),
        studio_id=tenant.get_studio_id(),
        host=host,
    )
    if not user_id:
        return _private(JSONResponse({"detail": "Invalid login handoff"}, status_code=403))

    response = JSONResponse({"redirect": "/admin"})
    name, value = security.set_session_cookie(user_id)
    response.set_cookie(
        name,
        value,
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    csrf_token = security.new_token()
    response.set_cookie(
        security.CSRF_COOKIE,
        security.sign(csrf_token),
        max_age=86400,
        httponly=False,
        secure=True,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(
        admin_oauth.NONCE_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return _private(response)
