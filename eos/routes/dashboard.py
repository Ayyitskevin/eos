import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import churn, config, listings, mailer, rebooking, security, studio
from ..render import templates
from ..vocab import LISTING_STATUSES

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request):
    today = dt.date.today()
    pipeline = listings.pipeline_counts()
    rows = listings.list_listings()
    due_soon = [
        r
        for r in rows
        if r["due_at"]
        and r["status"] not in ("delivered", "archived")
        and r["due_at"][:10] <= (today + dt.timedelta(days=2)).isoformat()
    ]
    rebooking_opportunities = rebooking.decorate_opportunities(churn.rebooking_opportunities())
    rebooking_followups = rebooking.follow_up_queue()
    rebooking_summary = rebooking.performance_snapshot(
        rebooking_opportunities, followups=rebooking_followups
    )
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "listings": rows,
            "pipeline": pipeline,
            "statuses": LISTING_STATUSES,
            "due_soon": due_soon,
            "today": today.isoformat(),
            "packages": studio.list_packages(),
            "presets": studio.list_crop_presets(),
            "base_url": config.BASE_URL,
            "rebooking_opportunities": rebooking_opportunities,
            "rebooking_followups": rebooking_followups,
            "rebooking_summary": rebooking_summary,
            "rebooking_mailer_on": mailer.configured(),
        },
    )
