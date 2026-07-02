from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from .. import (
    acquisition,
    brokerage,
    brokerage_reports,
    clients,
    reports,
    revenue_optimizer,
    security,
)
from ..render import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/reports", response_class=HTMLResponse)
async def reports_dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/reports.html",
        {
            "summary": reports.summary(),
            "top_agents": reports.top_agents(),
            "repeat_agents": reports.repeat_agent_revenue(),
            "repeat_agent_summary": reports.repeat_agent_summary(),
            "overdue": reports.overdue_invoices(),
            "brokerages": reports.brokerages_with_balance(),
            "client_list": clients.list_clients(),
        },
    )


@router.get("/reports/export.csv")
async def reports_csv(_: None = Depends(security.require_admin)):
    from .. import reports_export

    body = reports_export.full_csv()
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="eos-reports.csv"'},
    )


@router.get("/reports/repeat-agents.csv")
async def reports_repeat_agents_csv(_: None = Depends(security.require_admin)):
    from .. import reports_export

    body = reports_export.repeat_agents_csv()
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="eos-repeat-agents.csv"'},
    )


@router.get("/brokerages", response_class=HTMLResponse)
async def brokerage_accounts(request: Request):
    accounts = brokerage_reports.brokerage_accounts()
    return templates.TemplateResponse(
        request,
        "admin/brokerages.html",
        {
            "accounts": accounts,
            "summary": brokerage_reports.brokerage_summary(accounts),
        },
    )


@router.get("/brokerages.csv")
async def brokerage_accounts_csv(_: None = Depends(security.require_admin)):
    body = brokerage_reports.brokerage_accounts_csv()
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="eos-brokerages.csv"'},
    )


@router.get("/reports/revenue-optimizer", response_class=HTMLResponse)
async def revenue_optimizer_dashboard(request: Request):
    data = revenue_optimizer.dashboard()
    return templates.TemplateResponse(
        request,
        "admin/revenue_optimizer.html",
        data,
    )


@router.get("/reports/revenue-optimizer.csv")
async def revenue_optimizer_csv(_: None = Depends(security.require_admin)):
    body = revenue_optimizer.optimizer_csv()
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="eos-revenue-optimizer.csv"'},
    )


@router.get("/reports/acquisition", response_class=HTMLResponse)
async def acquisition_dashboard(request: Request):
    data = acquisition.dashboard()
    return templates.TemplateResponse(
        request,
        "admin/acquisition.html",
        data,
    )


@router.get("/reports/acquisition.csv")
async def acquisition_csv(_: None = Depends(security.require_admin)):
    body = acquisition.acquisition_csv()
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="eos-acquisition.csv"'},
    )


@router.get("/reports/export-quickbooks.csv")
async def reports_quickbooks(_: None = Depends(security.require_admin)):
    from .. import photographer_pay, reports_export

    body = reports_export.quickbooks_csv()
    pay_rows = photographer_pay.pay_report()
    if pay_rows:
        body += "\n\nPhotographer pay\n"
        import csv
        import io

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["shoot_date", "listing", "photographer", "pay_dollars"])
        for r in pay_rows:
            w.writerow(
                [
                    r["shoot_date"] or "",
                    r["title"],
                    r["photographer_name"] or "",
                    f"{(r['photographer_pay_cents'] or 0) / 100:.2f}",
                ]
            )
        body += buf.getvalue()
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="eos-quickbooks.csv"'},
    )


@router.get("/reports/brokerage/{client_id}", response_class=HTMLResponse)
async def brokerage_statement(request: Request, client_id: int):
    client = clients.get_client(client_id)
    return templates.TemplateResponse(
        request,
        "admin/brokerage_statement.html",
        {
            "client": client,
            "rows": brokerage.statement_rows(client_id),
            "totals": brokerage.statement_totals(client_id),
        },
    )
