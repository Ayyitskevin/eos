import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import appointments, db, security
from ..render import templates
from ..vocab import STUDIO_ID

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/today", response_class=HTMLResponse)
async def today_view(request: Request):
    today = dt.date.today().isoformat()
    shoots = db.all_(
        """SELECT a.*, l.title AS listing_title, c.name AS client_name
           FROM appointments a
           LEFT JOIN listings l ON l.id=a.listing_id
           LEFT JOIN clients c ON c.id=a.client_id
           WHERE a.studio_id=? AND a.status IN ('proposed','confirmed')
             AND a.starts_at LIKE ?
           ORDER BY a.starts_at""",
        (STUDIO_ID, f"{today}%"),
    )
    due_today = db.all_(
        """SELECT l.*, c.name AS client_name FROM listings l
           LEFT JOIN clients c ON c.id=l.client_id
           WHERE l.studio_id=? AND l.status NOT IN ('delivered','archived')
             AND l.due_at LIKE ?""",
        (STUDIO_ID, f"{today}%"),
    )
    pending_intake = db.all_(
        """SELECT q.*, l.title AS listing_title FROM questionnaires q
           JOIN listings l ON l.id=q.listing_id
           WHERE q.studio_id=? AND q.status='pending'
           ORDER BY q.sent_at DESC LIMIT 10""",
        (STUDIO_ID,),
    )
    return templates.TemplateResponse(
        request, "admin/today.html",
        {
            "today": today,
            "shoots": shoots,
            "due_today": due_today,
            "pending_intake": pending_intake,
            "upcoming": appointments.list_upcoming(days=7),
        },
    )