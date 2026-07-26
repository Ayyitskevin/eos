from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, mailer, security, sequences
from ..render import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/sequences", response_class=HTMLResponse)
async def sequences_index(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/sequences.html",
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


@router.post("/sequences/runs/{run_id}/retry")
async def retry_run(run_id: int):
    if not sequences.retry_run(run_id):
        raise HTTPException(
            status_code=409,
            detail="only definite failed sequence runs can be retried; reconcile unknown outcomes first",
        )
    return RedirectResponse("/admin/sequences", status_code=303)


@router.post("/sequences/runs/{run_id}/reconcile")
async def reconcile_run(run_id: int, outcome: str = Form(...)):
    if outcome not in {"delivered", "not_delivered"}:
        raise HTTPException(status_code=400, detail="invalid email reconciliation outcome")
    try:
        sequences.reconcile_run(run_id, delivered=outcome == "delivered")
    except sequences.SequenceRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except sequences.SequenceRunReconciliationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/admin/sequences", status_code=303)


@router.get("/sequences/{seq_id}/edit", response_class=HTMLResponse)
async def sequence_edit_form(request: Request, seq_id: int):
    seq = sequences.get_sequence(seq_id)
    return templates.TemplateResponse(
        request,
        "admin/sequence_edit.html",
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
