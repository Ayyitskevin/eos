from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import automations, brokerage, clients, config, invoices, listings, security
from ..render import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
async def invoice_detail(request: Request, invoice_id: int):
    inv = invoices.get_invoice(invoice_id)
    listing = listings.get_listing(inv["listing_id"]) if inv["listing_id"] else None
    bill_to = agent = None
    if inv["bill_to_client_id"]:
        bill_to = clients.get_client(inv["bill_to_client_id"])
    if inv["agent_client_id"]:
        agent = clients.get_client(inv["agent_client_id"])
    return templates.TemplateResponse(
        request,
        "admin/invoice.html",
        {
            "inv": inv,
            "listing": listing,
            "bill_to": bill_to,
            "agent": agent,
            "base_url": config.BASE_URL,
        },
    )


@router.post("/listings/{listing_id}/invoice")
async def create_listing_invoice(
    listing_id: int,
    title: str = Form(...),
    amount_dollars: str = Form(...),
    notes: str = Form(""),
    bill_to: str = Form("agent"),
):
    listing = listings.get_listing(listing_id)
    cents = int(round(float(amount_dollars) * 100))
    bill_to_id = agent_id = listing["client_id"]
    if listing["client_id"]:
        resolved_bill, resolved_agent = brokerage.resolve_billing(listing["client_id"])
        agent_id = resolved_agent
        bill_to_id = resolved_bill if bill_to == "brokerage" else listing["client_id"]
    iid = invoices.create_invoice(
        listing_id,
        title=title,
        amount_cents=cents,
        client_id=listing["client_id"],
        bill_to_client_id=bill_to_id,
        agent_client_id=agent_id,
        notes=notes,
    )
    return RedirectResponse(f"/admin/invoices/{iid}", status_code=303)


@router.post("/invoices/{invoice_id}/send")
async def send_invoice(invoice_id: int):
    invoices.mark_sent(invoice_id)
    return RedirectResponse(f"/admin/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id}/paid")
async def mark_paid(invoice_id: int):
    inv = invoices.get_invoice(invoice_id)
    invoices.mark_paid(invoice_id)
    automations.on_invoice_paid(inv["listing_id"])
    return RedirectResponse(f"/admin/invoices/{invoice_id}", status_code=303)
