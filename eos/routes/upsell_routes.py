"""Public upsell checkout from delivery surfaces."""

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, db, upsell
from ..render import templates
from ..vocab import STUDIO_ID

router = APIRouter()


@router.get("/upsell/{token}", response_class=HTMLResponse)
async def upsell_confirm(request: Request, token: str):
    row = db.one(
        """SELECT o.*, l.title AS listing_title FROM listing_upsell_orders o
           JOIN listings l ON l.id=o.listing_id
           WHERE o.token=? AND o.studio_id=?""",
        (token, str(STUDIO_ID)),
    )
    if not row:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "public/upsell.html",
        {
            "order": row,
            "payments_on": bool(config.STRIPE_SECRET_KEY),
        },
    )


@router.post("/g/{slug}/upsell")
async def gallery_upsell(slug: str, addon_ids: list[int] = Form(default=[])):
    from .. import galleries

    g = galleries.get_gallery_by_slug(slug)
    if not g["listing_id"]:
        raise HTTPException(status_code=400, detail="no listing linked")
    if not addon_ids:
        raise HTTPException(status_code=400, detail="select add-ons")
    result = upsell.create_order(listing_id=g["listing_id"], addon_ids=addon_ids)
    url = upsell.checkout_url(result["token"])
    return RedirectResponse(url, status_code=303)


@router.post("/upsell/{token}/pay")
async def upsell_pay(token: str):
    url = upsell.checkout_url(token)
    return RedirectResponse(url, status_code=303)