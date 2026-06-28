"""Platform super-admin routes — tenant list + impersonation."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import platform_admin, tenant
from ..render import templates

router = APIRouter(prefix="/admin/platform")


@router.get("/studios", response_class=HTMLResponse)
async def studios(request: Request, _: None = Depends(platform_admin.require_platform_admin)):
    return templates.TemplateResponse(
        request, "admin/platform_studios.html",
        {
            "studios": platform_admin.list_studios(),
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
    platform_admin.set_impersonation(resp, studio_id.strip())
    return resp


@router.post("/impersonate/clear")
async def clear_impersonate(
    _: None = Depends(platform_admin.require_platform_admin),
):
    resp = RedirectResponse("/admin/platform/studios", status_code=303)
    platform_admin.clear_impersonation(resp)
    return resp