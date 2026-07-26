"""Unified Stripe Checkout for invoices — Connect destination charges or legacy solo key."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging

import stripe
from fastapi import HTTPException

from . import config, db, stripe_connect, tenant

log = logging.getLogger("eos.stripe_checkout")


class PaymentTimingUnproven(RuntimeError):
    """A paid deposit Checkout lacks proof that it completed within its hold."""


def _line_items(*, title: str, amount_cents: int, currency: str) -> list:
    return [
        {
            "quantity": 1,
            "price_data": {
                "currency": currency,
                "unit_amount": amount_cents,
                "product_data": {"name": title},
            },
        }
    ]


def payment_rail() -> tuple[str, str | None]:
    """Return the only safe payment rail for the current studio."""
    conn = stripe_connect.studio_connect()
    if conn and conn["charges_enabled"] and stripe_connect.is_configured():
        return "connect", conn["account_id"]
    if config.STRIPE_SECRET_KEY and (not config.SAAS_MODE or tenant.get_studio_id() == "default"):
        return "legacy", None
    return "unavailable", None


def payments_configured() -> bool:
    return payment_rail()[0] != "unavailable"


def _bound_metadata(
    metadata: dict[str, str],
    *,
    rail: str,
    destination: str | None,
    currency: str,
) -> dict[str, str]:
    bound = {str(key): str(value) for key, value in metadata.items()}
    bound.update(
        {
            "studio_id": tenant.get_studio_id(),
            "payment_rail": rail,
            "stripe_destination_account": destination or "",
            "currency": currency,
        }
    )
    return bound


def _invoice_checkout_binding(
    metadata: dict[str, str],
    *,
    amount_cents: int,
    currency: str,
    destination: str | None,
    existing_session_id: str | None,
) -> tuple[int, str | None, int | None]:
    try:
        invoice_id = int(metadata["invoice_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invoice binding is required") from exc
    row = db.one(
        """SELECT i.studio_id, i.status, i.amount_cents, i.currency,
                  i.stripe_session_id, i.stripe_destination_account,
                  q.payment_expires_at
           FROM invoices i
           LEFT JOIN inquiries q
             ON q.id=i.inquiry_id AND q.studio_id=i.studio_id
           WHERE i.id=? AND i.studio_id=?""",
        (invoice_id, tenant.get_studio_id()),
    )
    if not row:
        raise HTTPException(status_code=404, detail="invoice not found")
    if row["status"] != "sent":
        raise HTTPException(status_code=409, detail="invoice is not payable")
    if row["amount_cents"] != amount_cents or row["currency"] != currency:
        raise HTTPException(status_code=409, detail="invoice amount binding changed")
    stored_session = row["stripe_session_id"]
    if existing_session_id and stored_session and existing_session_id != stored_session:
        raise HTTPException(status_code=409, detail="invoice checkout binding changed")
    stored_destination = row["stripe_destination_account"] or ""
    if stored_destination and stored_destination != (destination or ""):
        raise HTTPException(status_code=409, detail="invoice destination binding changed")
    checkout_expires_at = None
    if row["payment_expires_at"]:
        expires = dt.datetime.strptime(row["payment_expires_at"][:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=dt.UTC
        )
        remaining = (expires - dt.datetime.now(dt.UTC)).total_seconds()
        if remaining <= 0:
            from . import commerce

            commerce.expire_pending_bookings()
            raise HTTPException(status_code=409, detail="booking payment hold expired")
        if not (stored_session or existing_session_id) and remaining < 30 * 60:
            raise HTTPException(
                status_code=409,
                detail="booking payment hold is too close to expiry; please rebook",
            )
        checkout_expires_at = int(expires.timestamp())
    return invoice_id, stored_session or existing_session_id, checkout_expires_at


def _checkout_idempotency_key(
    *,
    invoice_id: int,
    rail: str,
    destination: str | None,
    amount_cents: int,
    currency: str,
    replacing_session_id: str | None,
) -> str:
    material = "\x1f".join(
        (
            tenant.get_studio_id(),
            str(invoice_id),
            rail,
            destination or "",
            str(amount_cents),
            currency,
            replacing_session_id or "initial",
        )
    )
    digest = hashlib.sha256(material.encode()).hexdigest()
    return f"eos-invoice-checkout-{digest}"


def _session_matches_binding(
    session,
    *,
    amount_cents: int,
    currency: str,
    metadata: dict[str, str],
    session_id: str | None = None,
) -> bool:
    session_metadata = dict(session.get("metadata") or {})
    return (
        (session_id is None or str(session.get("id") or "") == session_id)
        and session.get("mode") == "payment"
        and session.get("amount_total") == amount_cents
        and session.get("currency") == currency
        and all(session_metadata.get(key) == value for key, value in metadata.items())
    )


def _proven_paid_hold_event_created(invoice, session) -> float:
    """Return a conservative provider timestamp for a paid deposit Checkout."""
    if invoice["invoice_kind"] != "deposit" or not invoice["payment_expires_at"]:
        return dt.datetime.now(dt.UTC).timestamp()
    try:
        local_cutoff = dt.datetime.strptime(
            invoice["payment_expires_at"][:19], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=dt.UTC)
    except (TypeError, ValueError) as exc:
        raise PaymentTimingUnproven(
            "Paid Checkout timing is ambiguous because the booking cutoff is invalid; "
            "operator review of the signed Stripe event is required."
        ) from exc
    try:
        provider_expiry = float(session.get("expires_at"))
    except (TypeError, ValueError) as exc:
        raise PaymentTimingUnproven(
            "Paid Checkout timing is ambiguous because provider expiry proof is missing; "
            "operator review of the signed Stripe event is required."
        ) from exc
    if provider_expiry <= 0 or provider_expiry > local_cutoff.timestamp():
        raise PaymentTimingUnproven(
            "Paid Checkout provider expiry does not prove payment before the booking cutoff; "
            "operator review of the signed Stripe event is required."
        )
    return provider_expiry


def _validate_retrieved_paid_hold_timing(session) -> None:
    """Fail closed before stale-recovery callers can invent an on-time event."""
    if session.get("payment_status") != "paid":
        return
    metadata = session.get("metadata") or {}
    if not hasattr(metadata, "get"):
        return
    try:
        invoice_id = int(metadata.get("invoice_id"))
    except (TypeError, ValueError):
        return
    invoice = db.one(
        """SELECT i.invoice_kind, q.payment_expires_at
           FROM invoices i
           LEFT JOIN inquiries q
             ON q.id=i.inquiry_id AND q.studio_id=i.studio_id
           WHERE i.id=? AND i.studio_id=?""",
        (invoice_id, tenant.get_studio_id()),
    )
    if invoice:
        _proven_paid_hold_event_created(invoice, session)


def _persist_invoice_binding(
    metadata: dict[str, str],
    *,
    session_id: str,
    destination: str | None,
    currency: str,
) -> None:
    """Persist the binding only while the exact invoice remains payable."""
    try:
        invoice_id = int(metadata["invoice_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invoice binding is required") from exc
    with db.tx(immediate=True) as con:
        updated = con.execute(
            """UPDATE invoices
               SET stripe_session_id=?, stripe_destination_account=?, currency=?,
                   stripe_checkout_claimed_at=NULL
               WHERE id=? AND studio_id=? AND status='sent'""",
            (
                session_id,
                destination,
                currency,
                invoice_id,
                tenant.get_studio_id(),
            ),
        )
        if updated.rowcount != 1:
            raise HTTPException(status_code=409, detail="invoice is no longer payable")


def _claim_invoice_checkout(metadata: dict[str, str]) -> None:
    try:
        invoice_id = int(metadata["invoice_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invoice binding is required") from exc
    with db.tx(immediate=True) as con:
        claimed = con.execute(
            """UPDATE invoices
               SET stripe_checkout_claimed_at=COALESCE(
                   stripe_checkout_claimed_at, datetime('now')
               )
               WHERE id=? AND studio_id=? AND status='sent'""",
            (invoice_id, tenant.get_studio_id()),
        )
        if claimed.rowcount != 1:
            raise HTTPException(status_code=409, detail="invoice is no longer payable")


def create_payment_session(
    *,
    amount_cents: int,
    title: str,
    success_url: str,
    cancel_url: str,
    customer_email: str | None,
    metadata: dict[str, str],
    existing_session_id: str | None = None,
    currency: str = "usd",
) -> stripe.checkout.Session:
    currency = currency.strip().lower()
    if not currency:
        raise HTTPException(status_code=400, detail="invoice currency is required")

    rail, destination = payment_rail()
    if rail == "unavailable":
        raise HTTPException(status_code=503, detail="online payment is not configured")
    metadata = _bound_metadata(
        metadata,
        rail=rail,
        destination=destination,
        currency=currency,
    )
    invoice_id, existing_session_id, checkout_expires_at = _invoice_checkout_binding(
        metadata,
        amount_cents=amount_cents,
        currency=currency,
        destination=destination,
        existing_session_id=existing_session_id,
    )
    expiry_kwargs = {"expires_at": checkout_expires_at} if checkout_expires_at else {}
    idempotency_key = _checkout_idempotency_key(
        invoice_id=invoice_id,
        rail=rail,
        destination=destination,
        amount_cents=amount_cents,
        currency=currency,
        replacing_session_id=existing_session_id,
    )

    if existing_session_id:
        api_key = _api_key_for_retrieve(rail)
        if not api_key:
            raise HTTPException(status_code=503, detail="payment provider is unavailable")
        try:
            session = stripe.checkout.Session.retrieve(
                existing_session_id,
                api_key=api_key,
            )
            _validate_retrieved_paid_hold_timing(session)
        except PaymentTimingUnproven as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Unable to verify the existing checkout. Retry shortly.",
            ) from exc
        if not _session_matches_binding(
            session,
            amount_cents=amount_cents,
            currency=currency,
            metadata=metadata,
            session_id=existing_session_id,
        ):
            raise HTTPException(status_code=409, detail="stored checkout binding mismatch")
        provider_status = session.get("status")
        payment_status = session.get("payment_status")
        if payment_status == "paid":
            raise HTTPException(
                status_code=409,
                detail=(
                    "The existing Checkout is already paid; wait for the signed payment "
                    "event or ask an operator to reconcile it."
                ),
            )
        if provider_status == "open" and session.get("url"):
            _persist_invoice_binding(
                metadata,
                session_id=session.get("id"),
                destination=destination,
                currency=currency,
            )
            return session
        if provider_status != "expired" or payment_status != "unpaid":
            raise HTTPException(
                status_code=409,
                detail=(
                    "The existing Checkout is not provably expired and unpaid; "
                    "operator review is required before another payment attempt."
                ),
            )
    _claim_invoice_checkout(metadata)

    if rail == "connect" and destination:
        fee = stripe_connect.application_fee_cents(amount_cents)
        pi_data: dict = {"transfer_data": {"destination": destination}}
        if fee:
            pi_data["application_fee_amount"] = fee
        session = stripe.checkout.Session.create(
            api_key=stripe_connect.platform_api_key(),
            idempotency_key=idempotency_key,
            mode="payment",
            payment_method_types=["card"],
            line_items=_line_items(
                title=title,
                amount_cents=amount_cents,
                currency=currency,
            ),
            customer_email=customer_email,
            metadata=metadata,
            success_url=success_url,
            cancel_url=cancel_url,
            payment_intent_data=pi_data,
            **expiry_kwargs,
        )
        _persist_invoice_binding(
            metadata,
            session_id=session.id,
            destination=destination,
            currency=currency,
        )
        log.info("connect checkout %s amount=%s fee=%s", session.id, amount_cents, fee)
        return session

    if rail == "legacy":
        session = stripe.checkout.Session.create(
            api_key=config.STRIPE_SECRET_KEY,
            idempotency_key=idempotency_key,
            mode="payment",
            payment_method_types=["card"],
            line_items=_line_items(
                title=title,
                amount_cents=amount_cents,
                currency=currency,
            ),
            customer_email=customer_email,
            metadata=metadata,
            success_url=success_url,
            cancel_url=cancel_url,
            **expiry_kwargs,
        )
        _persist_invoice_binding(
            metadata,
            session_id=session.id,
            destination=None,
            currency=currency,
        )
        log.info("legacy checkout %s amount=%s", session.id, amount_cents)
        return session

    raise HTTPException(status_code=503, detail="online payment is not configured")


def _api_key_for_retrieve(rail: str | None = None) -> str:
    if rail is None:
        rail, _destination = payment_rail()
    if rail == "connect":
        return stripe_connect.platform_api_key()
    return config.STRIPE_SECRET_KEY if rail == "legacy" else ""


def retrieve_payment_session(session_id: str, *, destination: str | None = None):
    rail = "connect" if destination else "legacy"
    api_key = _api_key_for_retrieve(rail)
    if not api_key:
        raise RuntimeError("payment provider is unavailable")
    session = stripe.checkout.Session.retrieve(session_id, api_key=api_key)
    _validate_retrieved_paid_hold_timing(session)
    return session


def expire_payment_session(session_id: str, *, destination: str | None = None):
    rail = "connect" if destination else "legacy"
    api_key = _api_key_for_retrieve(rail)
    if not api_key:
        raise RuntimeError("payment provider is unavailable")
    return stripe.checkout.Session.expire(session_id, api_key=api_key)


def _reconcile_paid_for_manual_payment(invoice, session) -> tuple[str, str]:
    from . import stripe_webhooks

    session_id = str(session.get("id") or invoice["stripe_session_id"])
    try:
        event_created = _proven_paid_hold_event_created(invoice, session)
    except PaymentTimingUnproven as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    stripe_webhooks.handle_invoice_checkout_completed(
        session,
        event_id=f"manual-reconcile:{session_id}",
        event_created=event_created,
        source="stripe-manual-reconcile",
    )
    state = db.one(
        "SELECT status FROM invoices WHERE id=? AND studio_id=?",
        (invoice["id"], tenant.get_studio_id()),
    )
    if not state or state["status"] != "paid":
        raise HTTPException(
            status_code=409,
            detail="Provider payment could not be reconciled; review the payment event before retrying.",
        )
    return "paid", session_id


def prepare_manual_payment(invoice_id: int) -> tuple[str, str | None]:
    """Prove any provider Checkout is paid or unchargeable before a manual payment."""
    invoice = db.one(
        """SELECT i.*, q.payment_expires_at
           FROM invoices i
           LEFT JOIN inquiries q ON q.id=i.inquiry_id AND q.studio_id=i.studio_id
           WHERE i.id=? AND i.studio_id=?""",
        (invoice_id, tenant.get_studio_id()),
    )
    if not invoice:
        raise HTTPException(status_code=404)
    local_paid = invoice["status"] == "paid"
    if invoice["status"] not in ("sent", "paid"):
        raise HTTPException(status_code=409, detail="invoice is not payable")
    session_id = invoice["stripe_session_id"]
    if not session_id:
        if invoice["stripe_checkout_claimed_at"]:
            raise HTTPException(
                status_code=409,
                detail="Checkout creation is unresolved; reconcile Stripe before marking paid.",
            )
        return ("paid" if local_paid else "unpaid"), None

    destination = invoice["stripe_destination_account"] or None
    rail = "connect" if destination else "legacy"
    if config.SAAS_MODE and tenant.get_studio_id() != "default" and rail != "connect":
        raise HTTPException(
            status_code=409, detail="Legacy tenant Checkout requires operator reconciliation."
        )
    try:
        session = retrieve_payment_session(session_id, destination=destination)
    except PaymentTimingUnproven as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Unable to verify Checkout; no manual payment was recorded.",
        ) from exc
    metadata = _bound_metadata(
        {"invoice_id": str(invoice_id)},
        rail=rail,
        destination=destination,
        currency=invoice["currency"],
    )
    if not _session_matches_binding(
        session,
        amount_cents=invoice["amount_cents"],
        currency=invoice["currency"],
        metadata=metadata,
        session_id=session_id,
    ):
        raise HTTPException(status_code=409, detail="stored checkout binding mismatch")
    if session.get("payment_status") == "paid":
        if local_paid:
            raise HTTPException(
                status_code=409,
                detail="Stripe also reports this manually paid invoice as paid; review for a duplicate charge.",
            )
        return _reconcile_paid_for_manual_payment(invoice, session)
    if session.get("status") == "open":
        try:
            session = expire_payment_session(session_id, destination=destination)
        except Exception:
            try:
                session = retrieve_payment_session(session_id, destination=destination)
            except PaymentTimingUnproven as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Unable to expire Checkout; no manual payment was recorded.",
                ) from exc
    if not _session_matches_binding(
        session,
        amount_cents=invoice["amount_cents"],
        currency=invoice["currency"],
        metadata=metadata,
        session_id=session_id,
    ):
        raise HTTPException(status_code=409, detail="stored checkout binding mismatch")
    if session.get("payment_status") == "paid":
        if local_paid:
            raise HTTPException(
                status_code=409,
                detail="Stripe also reports this manually paid invoice as paid; review for a duplicate charge.",
            )
        return _reconcile_paid_for_manual_payment(invoice, session)
    if session.get("status") != "expired":
        raise HTTPException(
            status_code=409,
            detail="Checkout is not safely expired; no manual payment was recorded.",
        )
    return ("paid" if local_paid else "unpaid"), session_id
