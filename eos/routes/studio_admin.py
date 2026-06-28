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
            "addons": studio.list_addons(),
            "promos": studio.list_promo_codes(),
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
    booking_enabled: bool = Form(False),
    min_notice_hours: int = Form(24),
    buffer_minutes: int = Form(30),
    slot_minutes: int = Form(90),
    day_start_min: int = Form(480),
    day_end_min: int = Form(1080),
    book_weekdays: str = Form("0,1,2,3,4,5"),
):
    studio.update_studio(name=name, contact_email=contact_email)
    studio.update_profile(
        headline=headline, about=about, service_area=service_area, published=published,
        booking_enabled=booking_enabled, min_notice_hours=min_notice_hours,
        buffer_minutes=buffer_minutes, slot_minutes=slot_minutes,
        day_start_min=day_start_min, day_end_min=day_end_min,
        book_weekdays=book_weekdays.strip(),
    )
    return RedirectResponse("/admin/studio", status_code=303)


@router.post("/studio/packages/{package_id}")
async def package_update(
    package_id: int,
    name: str = Form(...),
    description: str = Form(""),
    price_dollars: float = Form(...),
    deposit_dollars: float = Form(0),
    turnaround_hours: int = Form(24),
    active: bool = Form(False),
):
    studio.update_package(
        package_id,
        name=name,
        description=description,
        price_cents=round(price_dollars * 100),
        deposit_cents=round(deposit_dollars * 100),
        turnaround_hours=turnaround_hours,
        active=active,
    )
    return RedirectResponse("/admin/studio", status_code=303)