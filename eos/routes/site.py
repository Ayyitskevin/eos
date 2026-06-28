from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import studio
from ..render import templates

router = APIRouter()
INDEXABLE = {"/", "/services"}


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    profile = studio.get_profile()
    packages = studio.list_packages()
    return templates.TemplateResponse(
        request, "site/home.html",
        {"profile": profile, "packages": packages},
    )