import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import listings, security
from ..render import templates
from ..vocab import LISTING_STATUSES

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/kanban", response_class=HTMLResponse)
async def kanban_board(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/kanban.html",
        {
            "board": listings.kanban_board(),
            "statuses": [s for s in LISTING_STATUSES if s != "archived"],
            "today": dt.date.today().isoformat(),
        },
    )


@router.post("/listings/{listing_id}/advance")
async def advance_listing(listing_id: int):
    listings.advance_status(listing_id)
    return RedirectResponse("/admin/kanban", status_code=303)
