"""Platform super-admin routes — tenant list, ops, impersonation."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import platform_admin, security, tenant
from ..render import templates

router = APIRouter(prefix="/admin/platform")


@router.get("/studios", response_class=HTMLResponse)
async def studios(request: Request, _: None = Depends(platform_admin.require_platform_admin)):
    return templates.TemplateResponse(
        request,
        "admin/platform_studios.html",
        {
            "studios": platform_admin.list_studios(include_inactive=True),
            "stats": platform_admin.platform_stats(),
            "impersonating": platform_admin.impersonated_studio_id(request),
            "current_studio": tenant.get_studio_id(),
        },
    )


@router.post("/impersonate")
async def impersonate(
    request: Request,
    studio_id: str = Form(...),
    _: None = Depends(platform_admin.require_platform_admin),
):
    resp = RedirectResponse("/admin", status_code=303)
    platform_admin.set_impersonation(
        resp,
        studio_id.strip(),
        admin_user_id=security.current_user_id(request),
    )
    return resp


@router.post("/impersonate/clear")
async def clear_impersonate(
    _: None = Depends(platform_admin.require_platform_admin),
):
    resp = RedirectResponse("/admin/platform/studios", status_code=303)
    platform_admin.clear_impersonation(resp)
    return resp


@router.post("/studios/{studio_id}/suspend")
async def suspend_studio(
    request: Request,
    studio_id: str,
    _: None = Depends(platform_admin.require_platform_admin),
):
    platform_admin.set_studio_active(
        studio_id,
        active=False,
        admin_user_id=security.current_user_id(request),
    )
    return RedirectResponse("/admin/platform/studios", status_code=303)


@router.post("/studios/{studio_id}/reactivate")
async def reactivate_studio(
    request: Request,
    studio_id: str,
    _: None = Depends(platform_admin.require_platform_admin),
):
    platform_admin.set_studio_active(
        studio_id,
        active=True,
        admin_user_id=security.current_user_id(request),
    )
    return RedirectResponse("/admin/platform/studios", status_code=303)


@router.post("/studios/{studio_id}/plan")
async def override_plan(
    request: Request,
    studio_id: str,
    plan_tier: str = Form(...),
    billing_status: str = Form(""),
    _: None = Depends(platform_admin.require_platform_admin),
):
    platform_admin.set_plan_tier(
        studio_id,
        plan_tier=plan_tier.strip(),
        billing_status=billing_status.strip() or None,
        admin_user_id=security.current_user_id(request),
    )
    return RedirectResponse("/admin/platform/studios", status_code=303)