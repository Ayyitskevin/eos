"""Expired booking holds reconcile provider truth before releasing local state."""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import eos.automations as automations
import eos.commerce as commerce
import eos.config as config
import eos.db as db
import eos.referrals as referrals
import eos.routes.invoices_admin as invoices_admin_routes
import eos.routes.studio_admin as studio_admin
import eos.stripe_checkout as stripe_checkout
import eos.stripe_webhooks as stripe_webhooks
import eos.tenant as tenant
import eos.upsell as upsell
import pytest
from fastapi import HTTPException
from starlette.requests import Request


@pytest.fixture()
def hold_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_BASE_URL", "http://testserver")
    monkeypatch.setenv("EOS_BASE_DOMAIN", "eos.test")
    monkeypatch.setenv("EOS_SAAS_MODE", "false")
    monkeypatch.setenv("EOS_STRIPE_SECRET_KEY", "sk_test_hold_recovery")
    monkeypatch.setenv("EOS_STRIPE_PLATFORM_SECRET_KEY", "")

    for module in (
        config,
        db,
        tenant,
        referrals,
        upsell,
        automations,
        stripe_checkout,
        stripe_webhooks,
        commerce,
        studio_admin,
    ):
        importlib.reload(module)

    config.ensure_dirs()
    db.migrate()
    tenant.set_studio("default")

    def blocked_provider_call(*_args, **_kwargs):
        raise AssertionError("an unmocked Stripe provider call escaped the test")

    monkeypatch.setattr(
        stripe_checkout.stripe.checkout.Session,
        "create",
        blocked_provider_call,
    )
    monkeypatch.setattr(
        stripe_checkout.stripe.checkout.Session,
        "retrieve",
        blocked_provider_call,
    )
    monkeypatch.setattr(
        stripe_checkout.stripe.checkout.Session,
        "expire",
        blocked_provider_call,
    )
    yield
    tenant.set_studio("default")


def _utc_sql(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


def _seed_hold(
    *,
    session_id: str | None = None,
    checkout_claimed: bool = False,
    referred: bool = False,
    credit_applied_cents: int = 0,
    expired_minutes_ago: int = 16,
) -> SimpleNamespace:
    now = dt.datetime.now(dt.UTC)
    payment_expires = now - dt.timedelta(minutes=expired_minutes_ago)
    client_id = db.run(
        """INSERT INTO clients
           (studio_id, name, email, credit_cents)
           VALUES ('default', 'Booking Client', 'client@example.test', 0)"""
    )
    referral_id = None
    if referred:
        referrer_id = db.run(
            """INSERT INTO clients
               (studio_id, name, email)
               VALUES ('default', 'Referring Client', 'referrer@example.test')"""
        )
        referral_id = db.run(
            """INSERT INTO referral_codes
               (studio_id, code, credit_cents, referrer_client_id, max_uses)
               VALUES ('default', 'REF-HOLD-TEST', 2500, ?, 3)""",
            (referrer_id,),
        )
    listing_id = db.run(
        """INSERT INTO listings
           (studio_id, client_id, title, status, address_line1)
           VALUES ('default', ?, '101 Hold Recovery Ave', 'lead', '101 Hold Recovery Ave')""",
        (client_id,),
    )
    appointment_id = db.run(
        """INSERT INTO appointments
           (studio_id, listing_id, client_id, title, status, starts_at, token)
           VALUES ('default', ?, ?, 'Listing shoot', 'proposed',
                   datetime('now', '+2 days'), 'appt-hold-recovery')""",
        (listing_id, client_id),
    )
    db.run(
        """INSERT INTO proposals
           (studio_id, listing_id, slug, title, line_items, total_cents, status)
           VALUES ('default', ?, 'proposal-hold-recovery', 'Photo services',
                   '[]', 50000, 'draft')""",
        (listing_id,),
    )
    inquiry_id = db.run(
        """INSERT INTO inquiries
           (studio_id, name, email, phone, property_address, status,
            addon_ids, scheduled_at, listing_id, client_id, appointment_id,
            order_token, signer_name, total_cents, deposit_cents, request_key,
            referral_id, payment_expires_at, credit_applied_cents)
           VALUES ('default', 'Booking Client', 'client@example.test', '',
                   '101 Hold Recovery Ave', 'pending_payment', '[]',
                   datetime('now', '+2 days'), ?, ?, ?, 'order-hold-recovery',
                   'Booking Client', 50000, 5000, 'request-hold-recovery',
                   ?, ?, ?)""",
        (
            listing_id,
            client_id,
            appointment_id,
            referral_id,
            _utc_sql(payment_expires),
            credit_applied_cents,
        ),
    )
    invoice_id = db.run(
        """INSERT INTO invoices
           (studio_id, listing_id, client_id, slug, title, amount_cents, status,
            line_items, invoice_kind, inquiry_id, currency, stripe_session_id,
            stripe_checkout_claimed_at)
           VALUES ('default', ?, ?, 'invoice-hold-recovery', 'Booking deposit',
                   5000, 'sent', '[]', 'deposit', ?, 'usd', ?,
                   CASE WHEN ? THEN datetime('now') ELSE NULL END)""",
        (
            listing_id,
            client_id,
            inquiry_id,
            session_id,
            1 if checkout_claimed else 0,
        ),
    )
    db.run(
        """UPDATE inquiries SET invoice_id=?
           WHERE id=? AND studio_id='default'""",
        (invoice_id, inquiry_id),
    )
    db.run(
        """UPDATE appointments SET inquiry_id=?
           WHERE id=? AND studio_id='default'""",
        (inquiry_id, appointment_id),
    )
    if credit_applied_cents:
        db.run(
            """INSERT INTO credit_ledger
               (studio_id, client_id, delta_cents, note)
               VALUES ('default', ?, ?, 'booking checkout')""",
            (client_id, -credit_applied_cents),
        )
    if referral_id:
        db.run(
            """INSERT INTO referral_redemptions
               (studio_id, referral_id, referral_code, inquiry_id,
                referred_client_id, credit_cents, status, expires_at)
               VALUES ('default', ?, 'REF-HOLD-TEST', ?, ?, 2500, 'reserved', ?)""",
            (
                referral_id,
                inquiry_id,
                client_id,
                _utc_sql(
                    payment_expires + dt.timedelta(minutes=commerce.PAYMENT_WEBHOOK_GRACE_MINUTES)
                ),
            ),
        )
    return SimpleNamespace(
        inquiry_id=inquiry_id,
        invoice_id=invoice_id,
        appointment_id=appointment_id,
        listing_id=listing_id,
        client_id=client_id,
        referral_id=referral_id,
        payment_expires=payment_expires,
        session_id=session_id,
    )


def _paid_session(hold: SimpleNamespace) -> dict:
    return {
        "id": hold.session_id,
        "mode": "payment",
        "status": "complete",
        "payment_status": "paid",
        "expires_at": int(hold.payment_expires.timestamp()),
        "amount_total": 5000,
        "currency": "usd",
        "metadata": {
            "invoice_id": str(hold.invoice_id),
            "studio_id": "default",
            "payment_rail": "legacy",
            "stripe_destination_account": "",
            "currency": "usd",
        },
    }


def _state(hold: SimpleNamespace) -> dict:
    inquiry = db.one(
        """SELECT status, payment_expired_at, payment_reconcile_attempts,
                  payment_reconcile_error, payment_reconciled_at
           FROM inquiries WHERE id=?""",
        (hold.inquiry_id,),
    )
    appointment = db.one(
        "SELECT status FROM appointments WHERE id=?",
        (hold.appointment_id,),
    )
    invoice = db.one(
        "SELECT status FROM invoices WHERE id=?",
        (hold.invoice_id,),
    )
    listing = db.one(
        "SELECT status FROM listings WHERE id=?",
        (hold.listing_id,),
    )
    return {
        "inquiry": dict(inquiry),
        "appointment": appointment["status"],
        "invoice": invoice["status"],
        "listing": listing["status"],
    }


def _operator_context(monkeypatch) -> dict:
    captured: dict = {}

    def capture_template(_request, _name, context):
        captured.update(context)
        return captured

    monkeypatch.setattr(studio_admin.templates, "TemplateResponse", capture_template)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/studio",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )
    return asyncio.run(studio_admin.studio_settings(request))


def test_expired_hold_without_checkout_releases_everything_exactly_once(
    hold_env,
    monkeypatch,
):
    hold = _seed_hold(referred=True, credit_applied_cents=2500)
    retrieve = Mock(side_effect=AssertionError("no Checkout session should be retrieved"))
    monkeypatch.setattr(stripe_checkout, "retrieve_payment_session", retrieve)
    expire = Mock(side_effect=AssertionError("no Checkout session should be expired"))
    monkeypatch.setattr(stripe_checkout, "expire_payment_session", expire)

    assert commerce.expire_pending_bookings() == 1
    assert commerce.expire_pending_bookings() == 0

    state = _state(hold)
    assert state["inquiry"]["status"] == "canceled"
    assert state["inquiry"]["payment_expired_at"] is not None
    assert state["inquiry"]["payment_reconcile_attempts"] == 1
    assert state["inquiry"]["payment_reconcile_error"] is None
    assert state["appointment"] == "canceled"
    assert state["invoice"] == "void"
    assert state["listing"] == "lead"
    assert (
        db.one("SELECT credit_cents FROM clients WHERE id=?", (hold.client_id,))["credit_cents"]
        == 2500
    )
    ledger = db.all_(
        """SELECT delta_cents FROM credit_ledger
           WHERE client_id=? ORDER BY id""",
        (hold.client_id,),
    )
    assert [row["delta_cents"] for row in ledger] == [-2500, 2500]
    redemption = db.one(
        "SELECT status FROM referral_redemptions WHERE inquiry_id=?",
        (hold.inquiry_id,),
    )
    assert redemption["status"] == "released"
    assert (
        db.one(
            "SELECT uses FROM referral_codes WHERE id=?",
            (hold.referral_id,),
        )["uses"]
        == 0
    )
    assert (
        db.one(
            """SELECT COUNT(*) AS n FROM audit_log
           WHERE action='booking.expire'"""
        )["n"]
        == 1
    )
    retrieve.assert_not_called()
    expire.assert_not_called()


def test_paid_checkout_reconciled_after_grace_confirms_referred_booking(
    hold_env,
    monkeypatch,
):
    hold = _seed_hold(session_id="cs_paid_before_expiry", referred=True)
    session = _paid_session(hold)

    def retrieve_session(session_id, *, api_key=None):
        assert not db.in_transaction()
        assert session_id == hold.session_id
        assert api_key == "sk_test_hold_recovery"
        return session

    retrieve = Mock(side_effect=retrieve_session)
    monkeypatch.setattr(stripe_checkout.stripe.checkout.Session, "retrieve", retrieve)
    expire = Mock(side_effect=AssertionError("a paid Checkout must not be expired"))
    monkeypatch.setattr(stripe_checkout, "expire_payment_session", expire)

    assert commerce.expire_pending_bookings() == 0

    state = _state(hold)
    assert state["inquiry"] == {
        "status": "confirmed",
        "payment_expired_at": None,
        "payment_reconcile_attempts": 1,
        "payment_reconcile_error": None,
        "payment_reconciled_at": state["inquiry"]["payment_reconciled_at"],
    }
    assert state["inquiry"]["payment_reconciled_at"] is not None
    assert state["appointment"] == "confirmed"
    assert state["invoice"] == "paid"
    assert state["listing"] == "booked"
    assert (
        db.one(
            "SELECT status FROM proposals WHERE listing_id=?",
            (hold.listing_id,),
        )["status"]
        == "sent"
    )
    redemption = db.one(
        "SELECT status FROM referral_redemptions WHERE inquiry_id=?",
        (hold.inquiry_id,),
    )
    assert redemption["status"] == "confirmed"
    assert (
        db.one(
            "SELECT uses FROM referral_codes WHERE id=?",
            (hold.referral_id,),
        )["uses"]
        == 1
    )
    receipt = db.one(
        """SELECT status FROM stripe_event_receipts
           WHERE source='stripe-reconcile' AND event_id=?""",
        (f"reconcile:{hold.session_id}",),
    )
    assert receipt["status"] == "processed"

    assert commerce.expire_pending_bookings() == 0
    assert (
        db.one(
            "SELECT uses FROM referral_codes WHERE id=?",
            (hold.referral_id,),
        )["uses"]
        == 1
    )
    retrieve.assert_called_once()
    expire.assert_not_called()


def test_open_unpaid_checkout_is_expired_outside_transaction_then_hold_cancels(
    hold_env,
    monkeypatch,
):
    hold = _seed_hold(session_id="cs_open_unpaid")

    def retrieve_session(session_id, *, destination=None):
        assert not db.in_transaction()
        assert session_id == hold.session_id
        assert destination is None
        return {
            "id": session_id,
            "status": "open",
            "payment_status": "unpaid",
        }

    def expire_session(session_id, *, destination=None):
        assert not db.in_transaction()
        assert session_id == hold.session_id
        assert destination is None
        return {
            "id": session_id,
            "status": "expired",
            "payment_status": "unpaid",
        }

    retrieve = Mock(side_effect=retrieve_session)
    expire = Mock(side_effect=expire_session)
    monkeypatch.setattr(stripe_checkout, "retrieve_payment_session", retrieve)
    monkeypatch.setattr(stripe_checkout, "expire_payment_session", expire)

    assert commerce.expire_pending_bookings() == 1

    state = _state(hold)
    assert state["inquiry"]["status"] == "canceled"
    assert state["inquiry"]["payment_reconcile_error"] is None
    assert state["appointment"] == "canceled"
    assert state["invoice"] == "void"
    retrieve.assert_called_once_with(hold.session_id, destination=None)
    expire.assert_called_once_with(hold.session_id, destination=None)


@pytest.mark.parametrize(
    ("provider_state", "expected_error"),
    [
        ("unknown", "unexpected Stripe Checkout status complete"),
        ("failure", "Stripe retrieval unavailable"),
    ],
)
def test_unknown_provider_state_keeps_hold_pending_and_visible_to_operator(
    hold_env,
    monkeypatch,
    provider_state,
    expected_error,
):
    hold = _seed_hold(session_id=f"cs_{provider_state}")

    def retrieve_session(session_id, *, destination=None):
        assert not db.in_transaction()
        assert session_id == hold.session_id
        assert destination is None
        if provider_state == "failure":
            raise RuntimeError("Stripe retrieval unavailable")
        return {
            "id": session_id,
            "status": "complete",
            "payment_status": "unpaid",
        }

    retrieve = Mock(side_effect=retrieve_session)
    expire = Mock(side_effect=AssertionError("unknown provider state must not be expired"))
    monkeypatch.setattr(stripe_checkout, "retrieve_payment_session", retrieve)
    monkeypatch.setattr(stripe_checkout, "expire_payment_session", expire)

    assert commerce.expire_pending_bookings() == 0

    state = _state(hold)
    assert state["inquiry"]["status"] == "pending_payment"
    assert state["inquiry"]["payment_expired_at"] is None
    assert state["inquiry"]["payment_reconcile_attempts"] == 1
    assert state["inquiry"]["payment_reconcile_error"] == expected_error
    assert state["inquiry"]["payment_reconciled_at"] is None
    assert state["appointment"] == "proposed"
    assert state["invoice"] == "sent"

    context = _operator_context(monkeypatch)
    visible = [row for row in context["pending_payment_holds"] if row["id"] == hold.inquiry_id]
    assert len(visible) == 1
    assert visible[0]["payment_reconcile_attempts"] == 1
    assert visible[0]["payment_reconcile_error"] == expected_error
    retrieve.assert_called_once()
    expire.assert_not_called()


def test_checkout_claim_without_persisted_session_never_auto_cancels(
    hold_env,
    monkeypatch,
):
    hold = _seed_hold(
        checkout_claimed=True,
        referred=True,
        credit_applied_cents=2500,
    )
    retrieve = Mock(side_effect=AssertionError("there is no persisted Checkout session"))
    monkeypatch.setattr(stripe_checkout, "retrieve_payment_session", retrieve)
    expire = Mock(side_effect=AssertionError("there is no persisted Checkout session"))
    monkeypatch.setattr(stripe_checkout, "expire_payment_session", expire)

    assert commerce.expire_pending_bookings() == 0
    assert commerce.expire_pending_bookings() == 0

    state = _state(hold)
    assert state["inquiry"]["status"] == "pending_payment"
    assert state["inquiry"]["payment_expired_at"] is None
    assert state["inquiry"]["payment_reconcile_attempts"] == 2
    assert state["inquiry"]["payment_reconcile_error"] == (
        "Checkout creation was claimed without a persisted session"
    )
    assert state["inquiry"]["payment_reconciled_at"] is None
    assert state["appointment"] == "proposed"
    assert state["invoice"] == "sent"
    assert (
        db.one("SELECT credit_cents FROM clients WHERE id=?", (hold.client_id,))["credit_cents"]
        == 0
    )
    assert (
        db.one(
            "SELECT status FROM referral_redemptions WHERE inquiry_id=?",
            (hold.inquiry_id,),
        )["status"]
        == "reserved"
    )
    assert (
        db.one(
            """SELECT COUNT(*) AS n FROM credit_ledger
           WHERE client_id=? AND delta_cents > 0""",
            (hold.client_id,),
        )["n"]
        == 0
    )
    retrieve.assert_not_called()
    expire.assert_not_called()


def test_paid_expired_reservation_keeps_last_finite_use_until_reconciled(
    hold_env,
):
    hold = _seed_hold(session_id="cs_paid_last_referral_use", referred=True)
    db.run(
        "UPDATE referral_codes SET max_uses=1 WHERE id=?",
        (hold.referral_id,),
    )
    competing_client_id = db.run(
        """INSERT INTO clients
           (studio_id, name, email)
           VALUES ('default', 'Competing Client', 'competitor@example.test')"""
    )
    competing_inquiry_id = db.run(
        """INSERT INTO inquiries
           (studio_id, name, email, status, addon_ids, client_id, order_token,
            signer_name, total_cents, deposit_cents, request_key, referral_id,
            payment_expires_at)
           VALUES ('default', 'Competing Client', 'competitor@example.test',
                   'pending_payment', '[]', ?, 'order-competing-referral',
                   'Competing Client', 50000, 5000, 'request-competing-referral',
                   ?, datetime('now', '+60 minutes'))""",
        (competing_client_id, hold.referral_id),
    )

    original = db.one(
        """SELECT status, expires_at FROM referral_redemptions
           WHERE inquiry_id=?""",
        (hold.inquiry_id,),
    )
    assert original["status"] == "reserved"
    assert (
        db.one(
            "SELECT datetime(?) <= datetime('now') AS expired",
            (original["expires_at"],),
        )["expired"]
        == 1
    )

    with pytest.raises(HTTPException) as exc_info:
        referrals.reserve_use(
            hold.referral_id,
            inquiry_id=competing_inquiry_id,
            referred_client_id=competing_client_id,
            expires_at=_utc_sql(dt.datetime.now(dt.UTC) + dt.timedelta(minutes=75)),
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Referral code is no longer available."
    redemptions = db.all_(
        """SELECT inquiry_id, status FROM referral_redemptions
           WHERE referral_id=? ORDER BY inquiry_id""",
        (hold.referral_id,),
    )
    assert [(row["inquiry_id"], row["status"]) for row in redemptions] == [
        (hold.inquiry_id, "reserved")
    ]
    assert (
        db.one(
            "SELECT uses FROM referral_codes WHERE id=?",
            (hold.referral_id,),
        )["uses"]
        == 0
    )

    event = {
        "id": "evt_paid_last_referral_use",
        "type": "checkout.session.completed",
        "created": (hold.payment_expires - dt.timedelta(seconds=1)).timestamp(),
        "data": {"object": _paid_session(hold)},
    }
    assert stripe_webhooks.handle_invoice_checkout_event(event) is True

    assert (
        db.one(
            "SELECT status FROM inquiries WHERE id=?",
            (hold.inquiry_id,),
        )["status"]
        == "confirmed"
    )
    redemptions = db.all_(
        """SELECT inquiry_id, status FROM referral_redemptions
           WHERE referral_id=? ORDER BY inquiry_id""",
        (hold.referral_id,),
    )
    assert [(row["inquiry_id"], row["status"]) for row in redemptions] == [
        (hold.inquiry_id, "confirmed")
    ]
    assert (
        db.one(
            "SELECT uses FROM referral_codes WHERE id=?",
            (hold.referral_id,),
        )["uses"]
        == 1
    )
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM referral_redemptions WHERE inquiry_id=?",
            (competing_inquiry_id,),
        )["n"]
        == 0
    )


@pytest.mark.asyncio
async def test_manual_mark_paid_rejects_expired_no_session_hold_in_final_transaction(
    hold_env,
):
    hold = _seed_hold(expired_minutes_ago=1)

    with pytest.raises(HTTPException, match="payment hold expired") as exc_info:
        await invoices_admin_routes.mark_paid(hold.invoice_id)

    assert exc_info.value.status_code == 409
    state = _state(hold)
    assert state["inquiry"]["status"] == "pending_payment"
    assert state["appointment"] == "proposed"
    assert state["invoice"] == "sent"
    assert state["listing"] == "lead"


@pytest.mark.parametrize(
    ("provider_expiry", "expected_error"),
    [
        (None, "provider expiry proof is missing"),
        ("after-cutoff", "does not prove payment before the booking cutoff"),
    ],
)
def test_ambiguous_legacy_paid_checkout_stays_pending_for_operator_review(
    hold_env,
    monkeypatch,
    provider_expiry,
    expected_error,
):
    hold = _seed_hold(session_id="cs_ambiguous_legacy_paid")
    session = _paid_session(hold)
    if provider_expiry is None:
        session.pop("expires_at")
    else:
        session["expires_at"] = int(hold.payment_expires.timestamp()) + 1
    retrieve = Mock(return_value=session)
    expire = Mock(side_effect=AssertionError("ambiguous paid Checkout must not be expired"))
    monkeypatch.setattr(stripe_checkout.stripe.checkout.Session, "retrieve", retrieve)
    monkeypatch.setattr(stripe_checkout, "expire_payment_session", expire)

    assert commerce.expire_pending_bookings() == 0

    state = _state(hold)
    assert state["inquiry"]["status"] == "pending_payment"
    assert state["inquiry"]["payment_reconcile_attempts"] == 1
    assert expected_error in state["inquiry"]["payment_reconcile_error"]
    assert "operator review" in state["inquiry"]["payment_reconcile_error"]
    assert state["appointment"] == "proposed"
    assert state["invoice"] == "sent"
    assert state["listing"] == "lead"
    retrieve.assert_called_once_with(hold.session_id, api_key="sk_test_hold_recovery")
    expire.assert_not_called()


def test_manual_paid_deposit_recovery_exposes_ambiguous_provider_timing(
    hold_env,
    monkeypatch,
):
    hold = _seed_hold(session_id="cs_manual_ambiguous_paid")
    session = _paid_session(hold)
    session.pop("expires_at")
    retrieve = Mock(return_value=session)
    monkeypatch.setattr(stripe_checkout.stripe.checkout.Session, "retrieve", retrieve)

    with pytest.raises(HTTPException, match="operator review") as exc_info:
        stripe_checkout.prepare_manual_payment(hold.invoice_id)

    assert exc_info.value.status_code == 409
    assert db.one("SELECT status FROM invoices WHERE id=?", (hold.invoice_id,))["status"] == "sent"
