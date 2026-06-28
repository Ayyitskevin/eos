from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import security, studio
from ..render import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/activity", response_class=HTMLResponse)
async def activity_log(request: Request):
    return templates.TemplateResponse(
        request, "admin/activity.html",
        {"rows": studio.list_activity()},
    )


@router.get("/sent", response_class=HTMLResponse)
async def sent_emails(request: Request):
    return templates.TemplateResponse(
        request, "admin/sent.html",
        {"rows": studio.list_emails()},
    )