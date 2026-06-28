from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, security, studio, users
from ..render import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/studio", response_class=HTMLResponse)
async def studio_settings(request: Request):
    return templates.TemplateResponse(
        request, "admin/studio.html",
        {
            "studio": studio.get_studio(),
            "profile": studio.get_profile(),
            "packages": studio.list_packages(),
            "presets": studio.list_crop_presets(),
            "inquiries": studio.list_inquiries(20),
            "operators": users.list_users(),
            "saas_mode": config.SAAS_MODE or bool(users.list_users()),
        },
    )


@router.post("/studio/users")
async def add_user(
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(""),
    role: str = Form("operator"),
):
    users.create_user(email, password, name=name, role=role)
    return RedirectResponse("/admin/studio", status_code=303)


@router.post("/studio")
async def studio_update(
    name: str = Form(...),
    contact_email: str = Form(""),
    headline: str = Form(""),
    about: str = Form(""),
    service_area: str = Form(""),
    published: bool = Form(False),
):
    studio.update_studio(name=name, contact_email=contact_email)
    studio.update_profile(headline=headline, about=about, service_area=service_area, published=published)
    return RedirectResponse("/admin/studio", status_code=303)