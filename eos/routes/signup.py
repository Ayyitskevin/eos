import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, invites, onboarding
from ..render import templates

router = APIRouter()
_SLUG_HINT = re.compile(r"^[a-z0-9-]+$")


def signup_context(*, error: str | None = None):
    return {
        "error": error,
        "base_domain": config.BASE_DOMAIN,
        "invite_required": invites.invite_required(),
    }


@router.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    if not config.SIGNUP_ENABLED:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "site/signup.html", signup_context())


@router.post("/signup")
async def signup_submit(
    request: Request,
    studio_name: str = Form(...),
    slug: str = Form(...),
    owner_name: str = Form(""),
    owner_email: str = Form(...),
    owner_password: str = Form(...),
    invite_code: str = Form(""),
):
    if not config.SIGNUP_ENABLED:
        raise HTTPException(status_code=404)
    try:
        result = onboarding.create_studio(
            name=studio_name,
            slug=slug,
            owner_email=owner_email,
            owner_password=owner_password,
            owner_name=owner_name,
            invite_code=invite_code,
        )
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else "Signup failed."
        return templates.TemplateResponse(
            request,
            "site/signup.html",
            signup_context(error=detail),
            status_code=e.status_code,
        )
    return RedirectResponse(result["login_url"], status_code=303)
