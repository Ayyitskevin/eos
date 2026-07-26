"""Online booking — package + slot + terms → listing, appointment, deposit invoice."""

import datetime as dt
import json
import logging

from fastapi import HTTPException

from . import appointments, clients, db, invoices, listings, proposals, scheduling, security
from .vocab import STUDIO_ID

log = logging.getLogger("eos.commerce")

BOOKING_TERMS = """\
By booking, you agree to {site_name}'s shoot terms: property access at the scheduled time,
cancellation within 24 hours may incur a fee, and payment of the deposit reserves your slot.
MLS/marketing usage rights apply as described in the photography services agreement.
"""


BOOKING_HOLD_MINUTES = 60


PAYMENT_WEBHOOK_GRACE_MINUTES = 15


def _session_value(session, key: str, default=None):
    if hasattr(session, "get"):
        return session.get(key, default)
    return getattr(session, key, default)


def _record_payment_reconcile_error(inquiry_id: int, error: str) -> None:
    db.run(
        """UPDATE inquiries
           SET payment_reconcile_attempts=payment_reconcile_attempts+1,
               payment_reconcile_error=?
           WHERE id=? AND studio_id=? AND status='pending_payment'""",
        (error[:500], inquiry_id, STUDIO_ID),
    )
    log.error("booking hold %s reconciliation blocked: %s", inquiry_id, error)


def _reconcile_paid_checkout(row, session) -> bool:
    from . import stripe_webhooks

    expires = dt.datetime.strptime(row["payment_expires_at"][:19], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=dt.UTC
    )
    session_id = str(_session_value(session, "id") or row["stripe_session_id"])
    stripe_webhooks.handle_invoice_checkout_completed(
        session,
        event_id=f"reconcile:{session_id}",
        event_created=expires.timestamp(),
        source="stripe-reconcile",
    )
    state = db.one(
        "SELECT status FROM inquiries WHERE id=? AND studio_id=?",
        (row["id"], STUDIO_ID),
    )
    confirmed = bool(state and state["status"] == "confirmed")
    if confirmed:
        db.run(
            """UPDATE inquiries
               SET payment_reconcile_attempts=payment_reconcile_attempts+1,
                   payment_reconcile_error=NULL, payment_reconciled_at=datetime('now')
               WHERE id=? AND studio_id=?""",
            (row["id"], STUDIO_ID),
        )
    return confirmed


def _provider_checkout_state(row):
    from . import stripe_checkout

    session = stripe_checkout.retrieve_payment_session(
        row["stripe_session_id"],
        destination=row["stripe_destination_account"],
    )
    if _session_value(session, "payment_status") == "paid":
        return "paid", session
    if _session_value(session, "status") == "expired":
        return "unpaid", session
    if _session_value(session, "status") != "open":
        raise RuntimeError(f"unexpected Stripe Checkout status {_session_value(session, 'status')}")
    try:
        session = stripe_checkout.expire_payment_session(
            row["stripe_session_id"],
            destination=row["stripe_destination_account"],
        )
    except Exception:
        session = stripe_checkout.retrieve_payment_session(
            row["stripe_session_id"],
            destination=row["stripe_destination_account"],
        )
    if _session_value(session, "payment_status") == "paid":
        return "paid", session
    if _session_value(session, "status") == "expired":
        return "unpaid", session
    raise RuntimeError("Stripe Checkout could not be safely expired")


def _cancel_expired_booking(row, *, grace: str) -> bool:
    with db.tx(immediate=True) as con:
        claimed = con.execute(
            """UPDATE inquiries
               SET status='canceled', payment_expired_at=datetime('now'),
                   payment_reconcile_attempts=payment_reconcile_attempts+1,
                   payment_reconcile_error=NULL, payment_reconciled_at=datetime('now')
               WHERE id=? AND studio_id=? AND status='pending_payment'
                 AND payment_expires_at <= datetime('now', ?)""",
            (row["id"], STUDIO_ID, grace),
        )
        if claimed.rowcount != 1:
            return False
        if row["appointment_id"]:
            con.execute(
                """UPDATE appointments SET status='canceled'
                   WHERE id=? AND studio_id=? AND status='proposed'""",
                (row["appointment_id"], STUDIO_ID),
            )
        if row["invoice_id"]:
            con.execute(
                """UPDATE invoices SET status='void'
                   WHERE id=? AND studio_id=? AND status='sent'""",
                (row["invoice_id"], STUDIO_ID),
            )
        if row["client_id"] and row["credit_applied_cents"]:
            con.execute(
                """UPDATE clients SET credit_cents=credit_cents+?
                   WHERE id=? AND studio_id=?""",
                (row["credit_applied_cents"], row["client_id"], STUDIO_ID),
            )
            con.execute(
                """INSERT INTO credit_ledger
                   (studio_id, client_id, delta_cents, note) VALUES (?,?,?,?)""",
                (
                    STUDIO_ID,
                    row["client_id"],
                    row["credit_applied_cents"],
                    f"booking hold expired inquiry={row['id']}",
                ),
            )
        con.execute(
            """UPDATE referral_redemptions SET status='released'
               WHERE studio_id=? AND inquiry_id=? AND status='reserved'""",
            (STUDIO_ID, row["id"]),
        )
    return True


def expire_pending_bookings(*, limit: int = 100) -> int:
    """Reconcile provider state, then release demonstrably unpaid holds."""
    grace = f"-{PAYMENT_WEBHOOK_GRACE_MINUTES} minutes"
    rows = db.all_(
        """SELECT q.id, q.appointment_id, q.invoice_id, q.client_id,
                  q.credit_applied_cents, q.payment_expires_at,
                  i.stripe_session_id, i.stripe_destination_account,
                  i.stripe_checkout_claimed_at
           FROM inquiries q
           LEFT JOIN invoices i ON i.id=q.invoice_id AND i.studio_id=q.studio_id
           WHERE q.studio_id=? AND q.status='pending_payment'
             AND q.payment_expires_at IS NOT NULL
             AND q.payment_expires_at <= datetime('now', ?)
           ORDER BY q.payment_expires_at LIMIT ?""",
        (STUDIO_ID, grace, limit),
    )
    expired = 0
    for row in rows:
        if row["stripe_session_id"]:
            try:
                state, session = _provider_checkout_state(row)
                if state == "paid":
                    if not _reconcile_paid_checkout(row, session):
                        raise RuntimeError("paid checkout did not confirm its booking")
                    continue
            except Exception as exc:
                _record_payment_reconcile_error(row["id"], str(exc))
                continue
        elif row["stripe_checkout_claimed_at"]:
            _record_payment_reconcile_error(
                row["id"], "Checkout creation was claimed without a persisted session"
            )
            continue
        if _cancel_expired_booking(row, grace=grace):
            expired += 1
    if expired:
        db.audit("system", "booking.expire", f"count={expired}")
        log.info("expired %s reconciled unpaid holds for studio %s", expired, STUDIO_ID)
    return expired


def expire_all_pending_bookings() -> int:
    from . import tenant

    grace = f"-{PAYMENT_WEBHOOK_GRACE_MINUTES} minutes"
    studios = db.all_(
        """SELECT DISTINCT studio_id FROM inquiries
           WHERE status='pending_payment' AND payment_expires_at IS NOT NULL
             AND payment_expires_at <= datetime('now', ?)""",
        (grace,),
    )
    previous = tenant.get_studio_id()
    total = 0
    try:
        for studio_row in studios:
            tenant.set_studio(studio_row["studio_id"])
            total += expire_pending_bookings()
    finally:
        tenant.set_studio(previous)
    return total


def _find_or_create_client(name: str, email: str, phone: str, *, client_type: str = "agent") -> int:
    from . import portal

    email = email.strip().lower()
    row = db.one(
        "SELECT id FROM clients WHERE studio_id=? AND lower(email)=?",
        (STUDIO_ID, email),
    )
    if row:
        portal.ensure_token(row["id"])
        return row["id"]
    return clients.create_client(name, email=email, phone=phone, client_type=client_type)


def _parse_address(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    if not raw:
        return "New listing", ""
    return raw, raw


def calc_total(
    package_id: int, addon_ids: list[int], promo: str = ""
) -> tuple[int, int, list[dict], int | None]:
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
        if len(addons) != len(addon_ids):
            raise HTTPException(status_code=400, detail="invalid add-on selection")
        for a in addons:
            items.append({"label": a["name"], "qty": 1, "unit_cents": a["price_cents"]})
            total += a["price_cents"]
    promo = promo.strip().upper()
    referral_row = None
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
        else:
            from . import referrals

            total, referral_row = referrals.apply_credit(promo, total)
    deposit = pkg["deposit_cents"] if pkg["deposit_cents"] > 0 else 0
    if deposit > total:
        deposit = total
    ref_id = referral_row["id"] if referral_row else None
    return total, deposit, items, ref_id


def _booking_result(row) -> dict:
    pay_slug = None
    if row["invoice_id"]:
        inv = db.one(
            "SELECT slug FROM invoices WHERE id=? AND studio_id=?",
            (row["invoice_id"], STUDIO_ID),
        )
        pay_slug = inv["slug"] if inv else None
    return {
        "inquiry_id": row["id"],
        "listing_id": row["listing_id"],
        "order_token": row["order_token"],
        "deposit_cents": row["deposit_cents"],
        "pay_slug": pay_slug,
        "confirmed": row["status"] == "confirmed",
    }


@db.transactional(immediate=True)
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
    client_type: str = "agent",
    credit_client_id: int | None = None,
    request_key: str = "",
) -> dict:
    request_key = request_key.strip() or security.new_token()
    if not 8 <= len(request_key) <= 128:
        raise HTTPException(status_code=400, detail="invalid booking request key")
    addon_ids = sorted(set(addon_ids or []))
    existing = db.one(
        """SELECT q.*, c.client_type AS existing_client_type
           FROM inquiries q
           LEFT JOIN clients c ON c.id=q.client_id AND c.studio_id=q.studio_id
           WHERE q.studio_id=? AND q.request_key=?""",
        (STUDIO_ID, request_key),
    )
    if existing:
        try:
            existing_addons = {int(value) for value in json.loads(existing["addon_ids"] or "[]")}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored booking request is invalid") from exc
        replay_matches = (
            (existing["name"] or "").strip() == name.strip()
            and (existing["email"] or "").strip().lower() == email.strip().lower()
            and (existing["phone"] or "").strip() == phone.strip()
            and (existing["property_address"] or "").strip() == property_address.strip()
            and existing["package_id"] == package_id
            and (existing["scheduled_at"] or "") == scheduled_at
            and existing_addons == set(addon_ids)
            and int(existing["sqft"] or 0) == int(sqft or 0)
            and (existing["message"] or "").strip() == message.strip()
            and (existing["signer_name"] or "").strip() == signer_name.strip()
            and (existing["promo_code"] or "").strip().upper() == promo_code.strip().upper()
            and (existing["existing_client_type"] or "agent") == client_type
        )
        if not replay_matches:
            raise HTTPException(
                status_code=409,
                detail="booking request key was already used for different details",
            )
        return _booking_result(existing)
    twilight = False
    if addon_ids:
        ph = ",".join("?" * len(addon_ids))
        twilight = bool(
            db.one(
                f"""SELECT 1 AS x FROM service_addons
                   WHERE id IN ({ph}) AND studio_id=? AND slug='twilight' LIMIT 1""",
                (*addon_ids, STUDIO_ID),
            )
        )
    if not scheduling.slot_is_open(scheduled_at, twilight=twilight):
        raise HTTPException(status_code=400, detail="slot no longer available")
    if not signer_name.strip():
        raise HTTPException(status_code=400, detail="signature required")
    pkg = db.one(
        "SELECT name, turnaround_hours FROM service_packages WHERE id=? AND studio_id=?",
        (package_id, STUDIO_ID),
    )

    client_id = _find_or_create_client(name, email, phone, client_type=client_type)
    total_cents, deposit_cents, line_items, referral_id = calc_total(
        package_id, addon_ids, promo_code
    )
    from . import credits

    total_cents, credit_applied = credits.apply_at_checkout(
        client_id if credit_client_id == client_id else None, total_cents
    )
    if credit_applied:
        line_items.append({"label": "Account credit", "qty": 1, "unit_cents": -credit_applied})
        deposit_cents = min(deposit_cents, total_cents)
    payment_required = bool(deposit_cents)
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
    appt_status = "proposed" if payment_required else "confirmed"
    db.run(
        "UPDATE appointments SET ends_at=?, status=? WHERE id=? AND studio_id=?",
        (ends, appt_status, appt_id, STUDIO_ID),
    )

    preset_key = pkg["name"].lower().replace(" ", "_")
    prop_id = proposals.create_proposal(listing_id, preset=preset_key)
    for a in addon_ids:
        addon = db.one(
            "SELECT name, price_cents FROM service_addons WHERE id=? AND studio_id=?",
            (a, STUDIO_ID),
        )
        if addon:
            prop = db.one(
                "SELECT line_items, total_cents FROM proposals WHERE id=? AND studio_id=?",
                (prop_id, STUDIO_ID),
            )
            items = json.loads(prop["line_items"] or "[]")
            items.append({"label": addon["name"], "qty": 1, "unit_cents": addon["price_cents"]})
            new_total = sum(i["qty"] * i["unit_cents"] for i in items)
            db.run(
                "UPDATE proposals SET line_items=?, total_cents=? WHERE id=? AND studio_id=?",
                (json.dumps(items), new_total, prop_id, STUDIO_ID),
            )
    if not payment_required:
        db.run(
            "UPDATE listings SET status='booked', updated_at=datetime('now') WHERE id=? AND studio_id=?",
            (listing_id, STUDIO_ID),
        )
        db.run(
            "UPDATE proposals SET status='sent', sent_at=datetime('now') WHERE id=? AND studio_id=?",
            (prop_id, STUDIO_ID),
        )

    token = security.new_token()
    inquiry_id = db.run(
        """INSERT INTO inquiries
           (studio_id, name, email, phone, message, property_address, status,
            package_id, addon_ids, sqft, scheduled_at, listing_id, client_id,
            appointment_id, order_token, signer_name, promo_code, total_cents, deposit_cents,
            request_key, referral_id, credit_applied_cents) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            STUDIO_ID,
            name.strip(),
            email.strip().lower(),
            phone.strip(),
            message.strip(),
            property_address.strip(),
            "pending_payment" if payment_required else "confirmed",
            package_id,
            json.dumps(addon_ids),
            sqft,
            scheduled_at,
            listing_id,
            client_id,
            appt_id,
            token,
            signer_name.strip(),
            promo_code.strip().upper(),
            total_cents,
            deposit_cents,
            request_key,
            referral_id,
            credit_applied,
        ),
    )
    if payment_required:
        db.run(
            """UPDATE inquiries
               SET payment_expires_at=datetime('now', ?)
               WHERE id=? AND studio_id=? AND status='pending_payment'""",
            (f"+{BOOKING_HOLD_MINUTES} minutes", inquiry_id, STUDIO_ID),
        )
    db.run(
        "UPDATE appointments SET inquiry_id=? WHERE id=? AND studio_id=?",
        (inquiry_id, appt_id, STUDIO_ID),
    )

    invoice_id = None
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
        db.run(
            "UPDATE inquiries SET invoice_id=? WHERE id=? AND studio_id=?",
            (invoice_id, inquiry_id, STUDIO_ID),
        )
    if referral_id:
        from . import referrals

        if payment_required:
            hold = db.one(
                """SELECT datetime(payment_expires_at, ?) AS referral_expires_at
                   FROM inquiries WHERE id=? AND studio_id=?""",
                (
                    f"+{PAYMENT_WEBHOOK_GRACE_MINUTES} minutes",
                    inquiry_id,
                    STUDIO_ID,
                ),
            )
            referrals.reserve_use(
                referral_id,
                inquiry_id=inquiry_id,
                referred_client_id=client_id,
                expires_at=hold["referral_expires_at"],
            )
        else:
            referrals.record_use(
                referral_id,
                inquiry_id=inquiry_id,
                referred_client_id=client_id,
            )
    if not payment_required:
        from . import automations

        automations.on_listing_booked(listing_id)
    db.audit("public", "booking.create", f"inquiry={inquiry_id} listing={listing_id}")
    log.info("booking %s listing %s deposit %s", inquiry_id, listing_id, deposit_cents)
    created = db.one("SELECT * FROM inquiries WHERE id=? AND studio_id=?", (inquiry_id, STUDIO_ID))
    return _booking_result(created)


def get_order_by_token(token: str):
    row = db.one(
        "SELECT * FROM inquiries WHERE order_token=? AND studio_id=?",
        (token, STUDIO_ID),
    )
    if not row:
        raise HTTPException(status_code=404)
    return row
