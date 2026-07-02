from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form
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
