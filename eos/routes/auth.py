import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import admin_oauth, config, db, security, tenant, users
from ..render import templates

log = logging.getLogger("eos.routes.auth")
router = APIRouter(prefix="/admin")


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    has_users = bool(db.one("SELECT 1 AS x FROM users WHERE active=1 LIMIT 1"))
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {
            "error": None,
            "saas_mode": config.SAAS_MODE or has_users,
            "google_login": admin_oauth.is_configured(),
            "google_login_url": admin_oauth.login_url(studio_id=tenant.get_studio_id())
            if admin_oauth.is_configured()
            else None,
        },
    )


def _saas_login(request: Request) -> bool:
    has_users = bool(db.one("SELECT 1 AS x FROM users WHERE active=1 LIMIT 1"))
    return config.SAAS_MODE or has_users


@router.post("/login")
async def login(
    request: Request,
    password: str = Form(...),
    email: str = Form(""),
):
    saas_mode = _saas_login(request)
    ip = security.client_ip(request)
    if security.pin_locked(ip, security.ADMIN_BUCKET):
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": "Locked out. Try again later.", "saas_mode": saas_mode},
            status_code=429,
        )
    email = email.strip().lower()
    user = None
    if email:
        user = users.authenticate(email, password, studio_id=tenant.get_studio_id())
        if not user:
            security.pin_fail(ip, security.ADMIN_BUCKET)
            return templates.TemplateResponse(
                request,
                "admin/login.html",
                {"error": "Wrong email or password.", "saas_mode": saas_mode},
                status_code=401,
            )
    elif security.legacy_admin_allowed():
        if not security.check_admin_password(password):
            security.pin_fail(ip, security.ADMIN_BUCKET)
            return templates.TemplateResponse(
                request,
                "admin/login.html",
                {"error": "Wrong password.", "saas_mode": saas_mode},
                status_code=401,
            )
    else:
        security.pin_fail(ip, security.ADMIN_BUCKET)
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": "Email is required.", "saas_mode": saas_mode},
            status_code=401,
        )

    security.pin_clear(ip, security.ADMIN_BUCKET)
    if user:
        tenant.set_studio(user["studio_id"])
    from .. import onboarding_wizard

    dest = "/admin"
    if user and onboarding_wizard.should_redirect() and not onboarding_wizard.status()["done"]:
        dest = "/admin/onboarding"
    resp = RedirectResponse(dest, status_code=303)
    name, value = security.set_session_cookie(user["id"] if user else None)
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
    log.info("admin login from %s (%s)", ip, email or "legacy")
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(security.ADMIN_COOKIE, path="/")
    return resp
