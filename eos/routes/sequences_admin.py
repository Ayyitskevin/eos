from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, mailer, security, sequences
from ..render import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/sequences", response_class=HTMLResponse)
async def sequences_index(request: Request):
    return templates.TemplateResponse(
        request, "admin/sequences.html",
        {
            "sequences": sequences.list_sequences(),
            "pending": sequences.list_pending_runs(),
            "mailer_on": mailer.configured(),
            "base_url": config.BASE_URL,
        },
    )


@router.post("/sequences/{seq_id}/toggle")
async def toggle_sequence(seq_id: int, active: bool = Form(False)):
    sequences.toggle_sequence(seq_id, active)
    return RedirectResponse("/admin/sequences", status_code=303)


@router.post("/sequences/runs/{run_id}/cancel")
async def cancel_run(run_id: int):
    sequences.cancel_run(run_id)
    return RedirectResponse("/admin/sequences", status_code=303)