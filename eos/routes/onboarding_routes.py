"""Onboarding wizard + signup verification pages."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import onboarding, onboarding_wizard, rbac, security, studio, tenant
from ..render import templates

router = APIRouter()


@router.get("/verify/{token}")
async def verify_signup(token: str):
    from .. import signup_verify

    slug = signup_verify.verify_token(token)
    return RedirectResponse(f"/admin/login?verified=1&studio={slug}", status_code=303)


def _masked_email(value: str) -> str:
    local, separator, domain = (value or "").partition("@")
    if not separator or not local or not domain:
        return ""
    return f"{local[0]}***@{domain}"


@router.get("/admin/verify-pending", response_class=HTMLResponse)
async def verify_pending(
    request: Request,
    _: None = Depends(rbac.require_owner),
):
    from .. import signup_verify

    contact_email = studio.get_studio()["contact_email"] or ""
    return templates.TemplateResponse(
        request,
        "admin/verify_pending.html",
        {
            "contact_email": _masked_email(contact_email),
            "verification_delivery": signup_verify.delivery_status(tenant.get_studio_id()),
        },
    )


@router.post("/admin/verify-pending/resend")
async def verify_resend(_: None = Depends(rbac.require_owner)):
    from .. import signup_verify

    signup_verify.resend(tenant.get_studio_id())
    return RedirectResponse("/admin/verify-pending?resent=1", status_code=303)


@router.post("/admin/verify-pending/reconcile")
async def verify_reconcile(
    outcome: str = Form(...),
    _: None = Depends(rbac.require_owner),
):
    from .. import signup_verify

    if outcome not in ("delivered", "not-delivered"):
        raise HTTPException(status_code=400, detail="Invalid provider outcome.")
    signup_verify.reconcile_delivery(
        tenant.get_studio_id(),
        delivered=outcome == "delivered",
    )
    return RedirectResponse(
        f"/admin/verify-pending?reconciled={outcome}",
        status_code=303,
    )


@router.get("/admin/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, _: None = Depends(security.require_admin)):
    return templates.TemplateResponse(
        request,
        "admin/onboarding.html",
        {"status": onboarding_wizard.status()},
    )


@router.post("/admin/onboarding/retry-provisioning")
async def onboarding_retry_provisioning(
    _: None = Depends(security.require_admin),
):
    onboarding.retry_provisioning()
    return RedirectResponse("/admin/onboarding?retried=1", status_code=303)


@router.post("/admin/onboarding/quick-launch")
async def onboarding_quick_launch(
    headline: str = Form(...),
    service_area: str = Form(...),
    _: None = Depends(security.require_admin),
):
    onboarding_wizard.quick_launch(headline=headline, service_area=service_area)
    if onboarding_wizard.status()["done"]:
        onboarding_wizard.advance(mark_done=True)
    return RedirectResponse("/admin/onboarding", status_code=303)


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
