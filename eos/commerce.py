"""Online booking — package + slot + terms → listing, appointment, deposit invoice."""

import json
import logging

from fastapi import HTTPException

from . import appointments, clients, db, invoices, listings, proposals, scheduling, security, studio
from .vocab import STUDIO_ID

log = logging.getLogger("eos.commerce")

BOOKING_TERMS = """\
By booking, you agree to {site_name}'s shoot terms: property access at the scheduled time,
cancellation within 24 hours may incur a fee, and payment of the deposit reserves your slot.
MLS/marketing usage rights apply as described in the photography services agreement.
"""


def _find_or_create_client(name: str, email: str, phone: str) -> int:
    email = email.strip().lower()
    row = db.one(
        "SELECT id FROM clients WHERE studio_id=? AND lower(email)=?",
        (STUDIO_ID, email),
    )
    if row:
        return row["id"]
    return clients.create_client(name, email=email, phone=phone, client_type="agent")


def _parse_address(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    if not raw:
        return "New listing", ""
    return raw, raw


def calc_total(package_id: int, addon_ids: list[int], promo: str = "") -> tuple[int, int, list[dict]]:
    pkg = db.one(
        "SELECT * FROM service_packages WHERE id=? AND studio_id=? AND active=1",
        (package_id, STUDIO_ID),
    )
    if not pkg:
        raise HTTPException(status_code=400, detail="invalid package")
    items = [{"label": pkg["name"], "qty": 1, "unit_cents": pkg["price_cents"]}]
    total = pkg["price_cents"]
    if addon_ids:
        placeholders = ",".join("?" * len(addon_ids))
        addons = db.all_(
            f"SELECT * FROM service_addons WHERE id IN ({placeholders}) AND studio_id=? AND active=1",
            (*addon_ids, STUDIO_ID),
        )
        for a in addons:
            items.append({"label": a["name"], "qty": 1, "unit_cents": a["price_cents"]})
            total += a["price_cents"]
    promo = promo.strip().upper()
    if promo:
        row = db.one(
            "SELECT * FROM promo_codes WHERE studio_id=? AND upper(code)=? AND active=1",
            (STUDIO_ID, promo),
        )
        if row:
            if row["discount_pct"]:
                total = max(0, total - (total * row["discount_pct"] // 100))
            else:
                total = max(0, total - row["discount_cents"])
    deposit = pkg["deposit_cents"] if pkg["deposit_cents"] > 0 else 0
    if deposit > total:
        deposit = total
    return total, deposit, items


def create_booking(
    *,
    name: str,
    email: str,
    phone: str,
    property_address: str,
    package_id: int,
    scheduled_at: str,
    addon_ids: list[int] | None = None,
    sqft: int | None = None,
    message: str = "",
    signer_name: str = "",
    promo_code: str = "",
) -> dict:
    if not scheduling.slot_is_open(scheduled_at):
        raise HTTPException(status_code=400, detail="slot no longer available")
    if not signer_name.strip():
        raise HTTPException(status_code=400, detail="signature required")

    addon_ids = addon_ids or []
    total_cents, deposit_cents, line_items = calc_total(package_id, addon_ids, promo_code)
    pkg = db.one("SELECT name, turnaround_hours FROM service_packages WHERE id=?", (package_id,))

    client_id = _find_or_create_client(name, email, phone)
    title, addr = _parse_address(property_address)
    listing_id = listings.create_listing(
        title,
        client_id=client_id,
        address_line1=addr,
        sqft=sqft,
        shoot_date=scheduled_at[:10],
        notes=message.strip(),
    )

    ends = scheduling.ends_at_for(scheduled_at)
    appt_id = appointments.create_appointment(
        title,
        kind="shoot",
        starts_at=scheduled_at,
        location=property_address.strip(),
        listing_id=listing_id,
        client_id=client_id,
    )
    appt_status = "confirmed" if deposit_cents == 0 else "proposed"
    db.run(
        "UPDATE appointments SET ends_at=?, status=? WHERE id=?",
        (ends, appt_status, appt_id),
    )

    preset_key = pkg["name"].lower().replace(" ", "_")
    prop_id = proposals.create_proposal(listing_id, preset=preset_key)
    for a in addon_ids:
        addon = db.one("SELECT name, price_cents FROM service_addons WHERE id=?", (a,))
        if addon:
            prop = db.one("SELECT line_items, total_cents FROM proposals WHERE id=?", (prop_id,))
            items = json.loads(prop["line_items"] or "[]")
            items.append({"label": addon["name"], "qty": 1, "unit_cents": addon["price_cents"]})
            new_total = sum(i["qty"] * i["unit_cents"] for i in items)
            db.run(
                "UPDATE proposals SET line_items=?, total_cents=? WHERE id=?",
                (json.dumps(items), new_total, prop_id),
            )
    if deposit_cents == 0:
        listings.update_listing(listing_id, status="booked")
        proposals.mark_sent(prop_id)

    token = security.new_token()
    inquiry_id = db.run(
        """INSERT INTO inquiries
           (studio_id, name, email, phone, message, property_address, status,
            package_id, addon_ids, sqft, scheduled_at, listing_id, client_id,
            appointment_id, order_token, signer_name, promo_code, total_cents, deposit_cents)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            STUDIO_ID, name.strip(), email.strip().lower(), phone.strip(), message.strip(),
            property_address.strip(), "pending_payment" if deposit_cents else "confirmed",
            package_id, json.dumps(addon_ids), sqft, scheduled_at, listing_id, client_id,
            appt_id, token, signer_name.strip(), promo_code.strip().upper(),
            total_cents, deposit_cents,
        ),
    )
    db.run("UPDATE appointments SET inquiry_id=? WHERE id=?", (inquiry_id, appt_id))

    invoice_id = None
    pay_slug = None
    if deposit_cents > 0:
        invoice_id = invoices.create_deposit_invoice(
            listing_id,
            client_id=client_id,
            inquiry_id=inquiry_id,
            amount_cents=deposit_cents,
            line_items=line_items,
            title=f"Booking deposit — {title}",
        )
        invoices.mark_sent(invoice_id)
        db.run("UPDATE inquiries SET invoice_id=? WHERE id=?", (invoice_id, inquiry_id))
        pay_slug = db.one("SELECT slug FROM invoices WHERE id=?", (invoice_id,))["slug"]


    db.audit("public", "booking.create", f"inquiry={inquiry_id} listing={listing_id}")
    log.info("booking %s listing %s deposit %s", inquiry_id, listing_id, deposit_cents)
    return {
        "inquiry_id": inquiry_id,
        "listing_id": listing_id,
        "order_token": token,
        "deposit_cents": deposit_cents,
        "pay_slug": pay_slug,
    }


def get_order_by_token(token: str):
    row = db.one(
        "SELECT * FROM inquiries WHERE order_token=? AND studio_id=?",
        (token, STUDIO_ID),
    )
    if not row:
        raise HTTPException(status_code=404)
    return row