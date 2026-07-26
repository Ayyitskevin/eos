"""Shared Stripe webhook handlers — client payments + platform billing."""

from __future__ import annotations

import datetime as dt
import logging

from . import automations, config, db, tenant, upsell

log = logging.getLogger("eos.stripe_webhooks")
_CHECKOUT_COMPLETED = "checkout.session.completed"
_RECEIPT_SOURCE = "stripe"


def handle_invoice_checkout_event(event: dict, *, source: str = _RECEIPT_SOURCE) -> bool:
    """Process one verified Stripe Checkout event with its immutable event ID."""
    if event.get("type") != _CHECKOUT_COMPLETED:
        return False
    data = event.get("data") or {}
    session = data.get("object") if hasattr(data, "get") else None
    if not session or not hasattr(session, "get"):
        return False
    return handle_invoice_checkout_completed(
        session,
        event_id=event.get("id"),
        event_type=event.get("type"),
        event_created=event.get("created"),
        source=source,
    )


def _reject_receipt(con, *, source: str, event_id: str, reason: str, studio_id: str | None) -> None:
    con.execute(
        """UPDATE stripe_event_receipts
           SET status='rejected', studio_id=?, error=?, updated_at=datetime('now')
           WHERE source=? AND event_id=?""",
        (studio_id, reason, source, event_id),
    )
    log.warning("stripe invoice event %s rejected: %s", event_id, reason)


def handle_invoice_checkout_completed(
    session: dict,
    *,
    event_id: str | None = None,
    event_type: str = _CHECKOUT_COMPLETED,
    event_created: int | float | None = None,
    source: str = _RECEIPT_SOURCE,
) -> bool:
    """Atomically claim a verified event and pay its exactly-bound invoice once."""
    event_id = str(event_id or "").strip()
    if not event_id:
        log.warning("stripe invoice checkout rejected without event id")
        return False
    try:
        event_created_ts = float(event_created) if event_created is not None else None
    except (TypeError, ValueError):
        event_created_ts = None

    metadata = session.get("metadata") or {}
    if not hasattr(metadata, "get"):
        metadata = {}
    metadata_studio = str(metadata.get("studio_id") or "")
    receipt_studio = metadata_studio or None
    raw_invoice_id = metadata.get("invoice_id")
    try:
        invoice_id = int(raw_invoice_id)
    except (TypeError, ValueError):
        invoice_id = None

    claimed_invoice: dict | None = None
    effect_error: Exception | None = None
    with db.tx(immediate=True) as con:
        receipt = con.execute(
            "SELECT status FROM stripe_event_receipts WHERE source=? AND event_id=?",
            (source, event_id),
        ).fetchone()
        if receipt:
            con.execute(
                """UPDATE stripe_event_receipts
                   SET attempts=attempts+1, updated_at=datetime('now')
                   WHERE source=? AND event_id=?""",
                (source, event_id),
            )
            if receipt["status"] != "failed":
                return False
            con.execute(
                """UPDATE stripe_event_receipts SET status='processing', error=NULL
                   WHERE source=? AND event_id=?""",
                (source, event_id),
            )

        if not receipt:
            con.execute(
                """INSERT INTO stripe_event_receipts
                   (source, event_id, event_type, studio_id)
                   VALUES (?,?,?,?)""",
                (source, event_id, event_type, receipt_studio),
            )

        reason: str | None = None
        invoice = None
        if event_type != _CHECKOUT_COMPLETED:
            reason = "unexpected_event_type"
        elif session.get("mode") != "payment":
            reason = "unexpected_checkout_mode"
        elif session.get("payment_status") != "paid":
            reason = "checkout_not_paid"
        elif invoice_id is None:
            reason = "invalid_invoice_id"
        else:
            invoice = con.execute(
                """SELECT i.id, i.listing_id, i.invoice_kind, i.inquiry_id,
                          i.studio_id, i.status, i.amount_cents, i.currency,
                          i.stripe_session_id, i.stripe_destination_account,
                          q.payment_expires_at
                   FROM invoices i
                   LEFT JOIN inquiries q
                     ON q.id=i.inquiry_id AND q.studio_id=i.studio_id
                   WHERE i.id=?""",
                (invoice_id,),
            ).fetchone()
            if not invoice:
                reason = "invoice_not_found"

        if invoice and reason is None:
            stored_destination = invoice["stripe_destination_account"] or ""
            expected_rail = "connect" if stored_destination else "legacy"
            metadata_destination = str(metadata.get("stripe_destination_account") or "")
            metadata_currency = str(metadata.get("currency") or "")
            session_id = str(session.get("id") or "")
            hold_reason = None
            if invoice["invoice_kind"] == "deposit" and invoice["payment_expires_at"]:
                if event_created_ts is None:
                    hold_reason = "missing_event_created"
                else:
                    try:
                        expires = dt.datetime.strptime(
                            invoice["payment_expires_at"][:19], "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=dt.UTC)
                    except (TypeError, ValueError):
                        hold_reason = "invalid_payment_expiry"
                    else:
                        if event_created_ts > expires.timestamp():
                            hold_reason = "payment_hold_expired"

            if hold_reason:
                reason = hold_reason
            elif invoice["status"] not in ("sent", "paid"):
                reason = "invoice_not_payable"
            elif session_id != invoice["stripe_session_id"]:
                reason = "stripe_session_mismatch"
            elif session.get("amount_total") != invoice["amount_cents"]:
                reason = "amount_mismatch"
            elif session.get("currency") != invoice["currency"]:
                reason = "currency_mismatch"
            elif metadata_currency != invoice["currency"]:
                reason = "metadata_currency_mismatch"
            elif metadata_studio != invoice["studio_id"]:
                reason = "studio_mismatch"
            elif metadata.get("payment_rail") != expected_rail:
                reason = "payment_rail_mismatch"
            elif metadata_destination != stored_destination:
                reason = "destination_mismatch"
            elif (
                config.SAAS_MODE
                and invoice["studio_id"] != "default"
                and expected_rail != "connect"
            ):
                reason = "saas_legacy_payment_forbidden"

        if reason is not None:
            _reject_receipt(
                con,
                source=source,
                event_id=event_id,
                reason=reason,
                studio_id=receipt_studio,
            )
            return False

        assert invoice is not None and invoice_id is not None
        if invoice["status"] == "paid":
            con.execute(
                """UPDATE stripe_event_receipts
                   SET status='processed', error=NULL, studio_id=?,
                       updated_at=datetime('now')
                   WHERE source=? AND event_id=?""",
                (invoice["studio_id"], source, event_id),
            )
            return False

        con.execute("SAVEPOINT invoice_effects")
        stored_destination = invoice["stripe_destination_account"] or ""
        if invoice["status"] == "sent":
            paid = con.execute(
                """UPDATE invoices
                   SET status='paid', paid_at=datetime('now')
                   WHERE id=? AND studio_id=? AND status='sent'
                     AND stripe_session_id=? AND amount_cents=? AND currency=?
                     AND COALESCE(stripe_destination_account, '')=?""",
                (
                    invoice_id,
                    invoice["studio_id"],
                    session.get("id"),
                    invoice["amount_cents"],
                    invoice["currency"],
                    stored_destination,
                ),
            )
            if paid.rowcount != 1:
                _reject_receipt(
                    con,
                    source=source,
                    event_id=event_id,
                    reason="invoice_compare_and_set_failed",
                    studio_id=invoice["studio_id"],
                )
                return False
        con.execute(
            """UPDATE stripe_event_receipts
               SET studio_id=?, updated_at=datetime('now')
               WHERE source=? AND event_id=?""",
            (invoice["studio_id"], source, event_id),
        )
        claimed_invoice = dict(invoice)

        previous_studio = tenant.get_studio_id()
        tenant.set_studio(claimed_invoice["studio_id"])
        try:
            upsell.mark_paid(invoice_id)
            if claimed_invoice["invoice_kind"] == "deposit" and claimed_invoice["inquiry_id"]:
                if not automations.on_deposit_paid(
                    claimed_invoice["inquiry_id"],
                    claimed_invoice["listing_id"],
                    payment_event_created=event_created_ts,
                ):
                    raise RuntimeError("deposit booking is no longer payable")
            elif claimed_invoice["listing_id"]:
                automations.on_invoice_paid(
                    claimed_invoice["listing_id"],
                    invoice_id=invoice_id,
                )
        except Exception as exc:
            con.execute("ROLLBACK TO invoice_effects")
            con.execute("RELEASE invoice_effects")
            con.execute(
                """UPDATE stripe_event_receipts
                   SET status='failed', error=?, updated_at=datetime('now')
                   WHERE source=? AND event_id=?""",
                (str(exc)[:1000], source, event_id),
            )
            effect_error = exc
        else:
            con.execute("RELEASE invoice_effects")
            con.execute(
                """UPDATE stripe_event_receipts
                   SET status='processed', error=NULL, updated_at=datetime('now')
                   WHERE source=? AND event_id=?""",
                (source, event_id),
            )
        finally:
            tenant.set_studio(previous_studio)

    if effect_error is not None:
        log.error("stripe invoice effects failed for event %s: %s", event_id, effect_error)
        raise effect_error
    log.info(
        "invoice %s paid via stripe checkout %s event=%s",
        invoice_id,
        session.get("id"),
        event_id,
    )
    return True
