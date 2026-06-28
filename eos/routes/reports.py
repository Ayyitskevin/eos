from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import brokerage, clients, reports, security
from ..render import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/reports", response_class=HTMLResponse)
async def reports_dashboard(request: Request):
    return templates.TemplateResponse(
        request, "admin/reports.html",
        {
            "summary": reports.summary(),
            "top_agents": reports.top_agents(),
            "overdue": reports.overdue_invoices(),
            "brokerages": reports.brokerages_with_balance(),
            "client_list": clients.list_clients(),
        },
    )


@router.get("/reports/brokerage/{client_id}", response_class=HTMLResponse)
async def brokerage_statement(request: Request, client_id: int):
    client = clients.get_client(client_id)
    return templates.TemplateResponse(
        request, "admin/brokerage_statement.html",
        {
            "client": client,
            "rows": brokerage.statement_rows(client_id),
            "totals": brokerage.statement_totals(client_id),
        },
    )