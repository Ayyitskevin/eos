"""Agent credit balances — ledger + booking application."""

from fastapi import HTTPException

from . import db
from .vocab import STUDIO_ID


def balance(client_id: int) -> int:
    row = db.one("SELECT credit_cents FROM clients WHERE id=? AND studio_id=?", (client_id, STUDIO_ID))
    if not row:
        raise HTTPException(status_code=404)
    return row["credit_cents"] or 0


def add_credit(client_id: int, *, amount_cents: int, note: str = "") -> int:
    if amount_cents <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    db.run(
        "UPDATE clients SET credit_cents=credit_cents+? WHERE id=? AND studio_id=?",
        (amount_cents, client_id, STUDIO_ID),
    )
    db.run(
        "INSERT INTO credit_ledger (studio_id, client_id, delta_cents, note) VALUES (?,?,?,?)",
        (STUDIO_ID, client_id, amount_cents, note.strip()),
    )
    db.audit("admin", "credit.add", f"client={client_id} cents={amount_cents}")
    return balance(client_id)


def apply_at_checkout(client_id: int | None, total_cents: int) -> tuple[int, int]:
    """Return (new_total, credit_applied)."""
    if not client_id or total_cents <= 0:
        return total_cents, 0
    bal = balance(client_id)
    if bal <= 0:
        return total_cents, 0
    applied = min(bal, total_cents)
    db.run(
        "UPDATE clients SET credit_cents=credit_cents-? WHERE id=? AND studio_id=?",
        (applied, client_id, STUDIO_ID),
    )
    db.run(
        "INSERT INTO credit_ledger (studio_id, client_id, delta_cents, note) VALUES (?,?,?,?)",
        (STUDIO_ID, client_id, -applied, "booking checkout"),
    )
    return total_cents - applied, applied


def ledger(client_id: int, *, limit: int = 20) -> list:
    return db.all_(
        """SELECT * FROM credit_ledger WHERE client_id=? AND studio_id=?
           ORDER BY created_at DESC LIMIT ?""",
        (client_id, STUDIO_ID, limit),
    )