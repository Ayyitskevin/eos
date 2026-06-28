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


@router.get("/sequences/{seq_id}/edit", response_class=HTMLResponse)
async def sequence_edit_form(request: Request, seq_id: int):
    seq = sequences.get_sequence(seq_id)
    return templates.TemplateResponse(
        request, "admin/sequence_edit.html",
        {"seq": seq, "triggers": sequences.TRIGGER_EVENTS},
    )


@router.post("/sequences/{seq_id}/edit")
async def sequence_edit_save(
    seq_id: int,
    name: str = Form(...),
    subject: str = Form(...),
    body_template: str = Form(...),
    delay_hours: int = Form(0),
    trigger_event: str = Form(...),
):
    sequences.update_sequence(
        seq_id,
        name=name,
        subject=subject,
        body_template=body_template,
        delay_hours=delay_hours,
        trigger_event=trigger_event,
    )
    return RedirectResponse("/admin/sequences", status_code=303)