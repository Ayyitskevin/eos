"""Brokerage billing — bill parent brokerage, attribute agent."""

from . import clients, db
from .vocab import STUDIO_ID


def resolve_billing(client_id: int | None) -> tuple[int | None, int | None]:
    """Return (bill_to_client_id, agent_client_id)."""
    if not client_id:
        return None, None
    client = clients.get_client(client_id)
    agent_id = client_id
    bill_to = client_id
    if client["parent_id"]:
        parent = clients.get_client(client["parent_id"])
        if parent["client_type"] == "brokerage":
            bill_to = parent["id"]
    return bill_to, agent_id


def statement_rows(brokerage_id: int) -> list:
    """Open and paid invoices billed to this brokerage, with agent attribution."""
    clients.get_client(brokerage_id)
    return db.all_(
        """SELECT i.*, l.title AS listing_title,
                  agent.name AS agent_name,
                  bill.name AS bill_to_name
           FROM invoices i
           LEFT JOIN listings l ON l.id=i.listing_id
           LEFT JOIN clients agent ON agent.id=i.agent_client_id
           LEFT JOIN clients bill ON bill.id=i.bill_to_client_id
           WHERE i.studio_id=? AND i.bill_to_client_id=?
             AND i.status IN ('sent','paid')
           ORDER BY i.created_at DESC""",
        (STUDIO_ID, brokerage_id),
    )


def statement_totals(brokerage_id: int) -> dict:
    rows = statement_rows(brokerage_id)
    open_cents = sum(r["amount_cents"] for r in rows if r["status"] == "sent")
    paid_cents = sum(r["amount_cents"] for r in rows if r["status"] == "paid")
    return {
        "open_cents": open_cents,
        "paid_cents": paid_cents,
        "n_open": sum(1 for r in rows if r["status"] == "sent"),
        "n_paid": sum(1 for r in rows if r["status"] == "paid"),
    }
