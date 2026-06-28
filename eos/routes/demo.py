"""Demo sandbox — read-only prospect environment."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, demo_sandbox, tenant
from ..render import templates

router = APIRouter()


@router.get("/demo", response_class=HTMLResponse)
async def demo_landing(request: Request):
    if not config.DEMO_ENABLED:
        return RedirectResponse("/", status_code=303)
    demo_sandbox.ensure_demo()
    url = demo_sandbox.demo_base_url()
    return templates.TemplateResponse(
        request,
        "public/demo.html",
        {"demo_url": url, "admin_hint": "demo@eos.app / demo-view-only"},
    )


@router.get("/demo/admin")
async def demo_admin_redirect():
    if not config.DEMO_ENABLED:
        return RedirectResponse("/", status_code=303)
    demo_sandbox.ensure_demo()
    tenant.set_studio(demo_sandbox.DEMO_STUDIO_ID)
    return RedirectResponse("/admin/login", status_code=303)
