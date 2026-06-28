"""Onboarding wizard + signup verification pages."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import onboarding_wizard, security, studio
from ..render import templates

router = APIRouter()


@router.get("/verify/{token}")
async def verify_signup(token: str):
    from .. import signup_verify

    slug = signup_verify.verify_token(token)
    return RedirectResponse(f"/admin/login?verified=1&studio={slug}", status_code=303)


@router.get("/admin/verify-pending", response_class=HTMLResponse)
async def verify_pending(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/verify_pending.html",
        {"contact_email": studio.get_studio().get("contact_email", "")},
    )


@router.get("/admin/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, _: None = Depends(security.require_admin)):
    return templates.TemplateResponse(
        request,
        "admin/onboarding.html",
        {"status": onboarding_wizard.status()},
    )


@router.post("/admin/onboarding")
async def onboarding_advance(
    _: None = Depends(security.require_admin),
    action: str = Form("next"),
):
    if action == "skip":
        onboarding_wizard.skip()
    else:
        st = onboarding_wizard.status()
        onboarding_wizard.advance(step=min(st["step"] + 1, len(st["steps"])))
        if onboarding_wizard.status()["progress_pct"] >= 100:
            onboarding_wizard.advance(mark_done=True)
    return RedirectResponse("/admin/onboarding", status_code=303)


@router.post("/admin/onboarding/done")
async def onboarding_done(_: None = Depends(security.require_admin)):
    onboarding_wizard.advance(mark_done=True)
    return RedirectResponse("/admin", status_code=303)
