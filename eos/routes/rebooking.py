from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse

from .. import rebooking, security

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


def _safe_redirect(path: str, client_id: int) -> str:
    if path.startswith("/admin"):
        return path
    return f"/admin/clients/{client_id}"


def _with_notice(path: str, *, status: str, client_id: int) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}{urlencode({'rebooking': status, 'client_id': client_id})}"


@router.post("/rebooking/{client_id}/send")
async def rebooking_send(client_id: int, redirect: str = Form("")):
    result = rebooking.send_email(client_id)
    target = _safe_redirect(redirect, client_id)
    return RedirectResponse(
        _with_notice(target, status=result["status"], client_id=client_id),
        status_code=303,
    )


@router.post("/rebooking/{client_id}/intents/{intent_id}/reconcile")
async def rebooking_reconcile(client_id: int, intent_id: int, outcome: str = Form(...)):
    if outcome not in {"delivered", "not-delivered"}:
        raise HTTPException(status_code=400, detail="invalid rebooking reconciliation outcome")
    rebooking.reconcile_intent(
        intent_id,
        client_id=client_id,
        delivered=outcome == "delivered",
    )
    notice = "reconciled-sent" if outcome == "delivered" else "retry-ready"
    return RedirectResponse(
        _with_notice(f"/admin/clients/{client_id}", status=notice, client_id=client_id),
        status_code=303,
    )
