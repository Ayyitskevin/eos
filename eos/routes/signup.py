import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, mailer, onboarding, security
from ..render import templates

router = APIRouter()
_SLUG_HINT = re.compile(r"^[a-z0-9-]+$")


@router.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    if not config.SIGNUP_ENABLED:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "site/signup.html",
        {"error": None, "base_domain": config.BASE_DOMAIN},
    )


@router.post("/signup")
async def signup_submit(
    request: Request,
    studio_name: str = Form(...),
    slug: str = Form(...),
    owner_name: str = Form(""),
    owner_email: str = Form(...),
    owner_password: str = Form(...),
):
    if not config.SIGNUP_ENABLED:
        raise HTTPException(status_code=404)
    ip = security.client_ip(request)
    if security.signup_throttled(ip):
        return templates.TemplateResponse(
            request, "site/signup.html",
            {"error": "Too many signups from this network. Try again later.", "base_domain": config.BASE_DOMAIN},
            status_code=429,
        )
    try:
        result = onboarding.create_studio(
            name=studio_name,
            slug=slug,
            owner_email=owner_email,
            owner_password=owner_password,
            owner_name=owner_name,
        )
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else "Signup failed."
        return templates.TemplateResponse(
            request, "site/signup.html",
            {"error": detail, "base_domain": config.BASE_DOMAIN},
            status_code=e.status_code,
        )
    security.signup_record(ip)
    if config.SIGNUP_ENABLED and mailer.configured():
        return RedirectResponse(f"{config.BASE_URL}/admin/verify-pending", status_code=303)
    return RedirectResponse(result["login_url"], status_code=303)