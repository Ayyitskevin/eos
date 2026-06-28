import datetime as dt

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, galleries, security
from ..render import templates

router = APIRouter()


def _check_expiry(g) -> None:
    if g["expires_at"] and g["expires_at"] < dt.date.today().isoformat():
        raise HTTPException(status_code=410)


@router.get("/g/{slug}", response_class=HTMLResponse)
async def gallery_gate(request: Request, slug: str):
    g = galleries.get_gallery_by_slug(slug)
    if not g["published"]:
        raise HTTPException(status_code=404)
    _check_expiry(g)
    if security.gallery_unlocked(request, g["id"]):
        return await gallery_view(request, slug)
    return templates.TemplateResponse(
        request, "public/pin.html",
        {"g": g, "error": None},
    )


@router.post("/g/{slug}/pin")
async def gallery_pin(request: Request, slug: str, pin: str = Form(...)):
    g = galleries.get_gallery_by_slug(slug)
    _check_expiry(g)
    ip = security.client_ip(request)
    if security.pin_locked(ip, g["id"]):
        return templates.TemplateResponse(
            request, "public/pin.html",
            {"g": g, "error": "Too many attempts. Try again later."},
            status_code=429,
        )
    if pin.strip() != g["pin"]:
        security.pin_fail(ip, g["id"])
        return templates.TemplateResponse(
            request, "public/pin.html",
            {"g": g, "error": "Wrong PIN."},
            status_code=401,
        )
    security.pin_clear(ip, g["id"])
    resp = RedirectResponse(f"/g/{slug}", status_code=303)
    name, value = security.set_gallery_cookie(g["id"])
    resp.set_cookie(
        name, value, max_age=config.SESSION_MAX_AGE, httponly=True,
        secure=config.COOKIE_SECURE, samesite="lax", path="/",
    )
    return resp


async def gallery_view(request: Request, slug: str):
    g = galleries.get_gallery_by_slug(slug)
    sections, by_section, unsectioned = galleries.assets_by_section(g["id"])
    return templates.TemplateResponse(
        request, "public/gallery.html",
        {"g": g, "sections": sections, "by_section": by_section,
         "unsectioned": unsectioned, "assets": galleries.gallery_assets(g["id"])},
    )