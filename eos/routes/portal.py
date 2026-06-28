"""Public agent portal — deliveries and invoice links."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import config, portal
from ..render import templates

router = APIRouter()


@router.get("/portal/{token}", response_class=HTMLResponse)
async def agent_portal(request: Request, token: str):
    client = portal.get_client_by_token(token)
    rows = portal.deliveries(client["id"])
    return templates.TemplateResponse(
        request, "public/portal.html",
        {
            "client": client,
            "deliveries": rows,
            "base_url": config.BASE_URL,
            "payments_on": bool(config.STRIPE_SECRET_KEY),
        },
    )