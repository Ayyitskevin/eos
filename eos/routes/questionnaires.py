import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import listings, questionnaires, security
from ..render import templates
from ..vocab import QUESTIONNAIRE_FIELDS

router = APIRouter()
admin = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@admin.post("/listings/{listing_id}/questionnaire")
async def create_questionnaire(listing_id: int):
    listings.get_listing(listing_id)
    questionnaires.create_for_listing(listing_id)
    return RedirectResponse(f"/admin/listings/{listing_id}#questionnaire", status_code=303)


@router.get("/q/{token}", response_class=HTMLResponse)
async def questionnaire_form(request: Request, token: str):
    q = questionnaires.get_by_token(token)
    listing = listings.get_listing(q["listing_id"])
    answers = json.loads(q["answers"] or "{}")
    return templates.TemplateResponse(
        request,
        "public/questionnaire.html",
        {
            "q": q,
            "listing": listing,
            "fields": QUESTIONNAIRE_FIELDS,
            "answers": answers,
            "address": listings.format_address(listing),
        },
    )


@router.post("/q/{token}")
async def questionnaire_submit(request: Request, token: str):
    form = await request.form()
    answers = {k: form.get(k, "") for k in QUESTIONNAIRE_FIELDS}
    questionnaires.save_answers(token, answers)
    return RedirectResponse(f"/q/{token}", status_code=303)
