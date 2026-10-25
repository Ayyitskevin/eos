"""Real Stripe resources at Eos boundaries, with synthetic signed webhook bodies."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import time
from unittest.mock import Mock

import eos.automations as automations
import eos.commerce as commerce
import eos.config as config
import eos.db as db
import eos.platform_billing as platform_billing
import eos.stripe_checkout as stripe_checkout
import eos.stripe_webhooks as stripe_webhooks
import eos.tenant as tenant
import eos.upsell as upsell
import pytest
import stripe
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

KEY = "sk_test_synthetic_sdk_objects"
SECRET = "whsec_synthetic_sdk_objects"


@pytest.fixture()
def sdk_env(app_env_http, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", KEY)
    monkeypatch.setattr(config, "STRIPE_PLATFORM_SECRET_KEY", KEY)
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "STRIPE_PLATFORM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "STRIPE_PRICE_PRO", "price_synthetic")
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    tenant.set_studio("default")
    # Never let a missing fixture issue a provider request.
    monkeypatch.setattr(
        stripe.HTTPClient,
        "request_with_retries",
        Mock(side_effect=AssertionError("unexpected Stripe transport call")),
    )
    yield app_env_http
    tenant.set_studio("default")


def _invoice():
    listing = db.run("INSERT INTO listings (studio_id,title) VALUES ('default','Synthetic')")
    return db.run(
        """INSERT INTO invoices
           (studio_id,listing_id,slug,title,amount_cents,status,line_items,
            invoice_kind,currency,stripe_session_id)
           VALUES ('default',?,'sdk-invoice','Synthetic fee',5000,'sent','[]',
                   'full','usd','cs_synthetic')""",
        (listing,),
    )


def _session(invoice_id, **changes):
    data = {
        "object": "checkout.session",
        "id": "cs_synthetic",
        "url": "https://checkout.example.invalid/synthetic",
        "mode": "payment",
        "status": "open",
        "payment_status": "unpaid",
        "amount_total": 5000,
        "currency": "usd",
        "metadata": {
            "invoice_id": str(invoice_id),
            "studio_id": "default",
            "payment_rail": "legacy",
            "stripe_destination_account": "",
            "currency": "usd",
        },
    }
    data.update(changes)
    session = stripe.checkout.Session.construct_from(data, KEY)
    assert isinstance(session.metadata, stripe.StripeObject)
    return session


def _event(obj, *, kind="checkout.session.completed", created=None):
    return {
        "id": "evt_synthetic",
        "object": "event",
        "type": kind,
        "created": int(time.time()) if created is None else created,
        "data": {"object": obj.to_dict()},
    }


async def _post_event(app, path, event, *, valid=True):
    payload = json.dumps(event).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        SECRET.encode(), timestamp.encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    if not valid:
        signature = "0" * 64
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        return await client.post(
            path, content=payload, headers={"stripe-signature": f"t={timestamp},v1={signature}"}
        )


def test_retrieval_and_expiry_return_recursive_dicts(sdk_env, monkeypatch):
    invoice_id = _invoice()
    session = _session(invoice_id)
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", Mock(return_value=session))
    expired = _session(invoice_id, status="expired")
    monkeypatch.setattr(stripe.checkout.Session, "expire", Mock(return_value=expired))
    for result in (
        stripe_checkout.retrieve_payment_session("cs_synthetic"),
        stripe_checkout.expire_payment_session("cs_synthetic"),
    ):
        assert isinstance(result, dict)
        assert isinstance(result["metadata"], dict)
        assert result["metadata"]["invoice_id"] == str(invoice_id)
    assert session.id == "cs_synthetic"  # Conversion must not mutate SDK resources.


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_public_checkout_keeps_resource_attribute_contract(sdk_env, monkeypatch, existing):
    invoice_id = _invoice()
    if not existing:
        db.run("UPDATE invoices SET stripe_session_id=NULL WHERE id=?", (invoice_id,))
    session = _session(invoice_id)
    create = Mock(return_value=session)
    retrieve = Mock(return_value=session)
    monkeypatch.setattr(stripe.checkout.Session, "create", create)
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", retrieve)
    async with AsyncClient(
        transport=ASGITransport(app=sdk_env), base_url="https://testserver"
    ) as client:
        response = await client.post("/i/sdk-invoice/pay", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == session.url
    assert create.call_count == (0 if existing else 1)
    assert retrieve.call_count == (1 if existing else 0)


@pytest.mark.parametrize("changed", ["amount", "tenant"])
def test_reused_resource_preserves_invoice_binding_rejection(sdk_env, monkeypatch, changed):
    invoice_id = _invoice()
    session = _session(invoice_id)
    if changed == "amount":
        session.amount_total = 4999
    else:
        session.metadata.studio_id = "other-studio"
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", Mock(return_value=session))
    with pytest.raises(HTTPException) as exc:
        stripe_checkout.create_payment_session(
            amount_cents=5000,
            title="Synthetic",
            success_url="https://example.invalid/ok",
            cancel_url="https://example.invalid/cancel",
            customer_email=None,
            metadata={"invoice_id": str(invoice_id)},
        )
    assert exc.value.status_code == 409
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "sent"


@pytest.mark.parametrize("expire_error", [False, True])
def test_manual_reconciliation_handles_real_expiry_and_recovery(sdk_env, monkeypatch, expire_error):
    invoice_id = _invoice()
    opened = _session(invoice_id)
    expired = _session(invoice_id, status="expired")
    retrieve = Mock(side_effect=[opened, expired] if expire_error else [opened])
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", retrieve)
    monkeypatch.setattr(
        stripe.checkout.Session,
        "expire",
        Mock(side_effect=RuntimeError("synthetic timeout"))
        if expire_error
        else Mock(return_value=expired),
    )
    assert stripe_checkout.prepare_manual_payment(invoice_id) == ("unpaid", "cs_synthetic")
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "sent"


def test_manual_paid_reconciliation_uses_nested_resource_metadata(sdk_env, monkeypatch):
    invoice_id = _invoice()
    session = _session(invoice_id, status="complete", payment_status="paid")
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", Mock(return_value=session))
    paid = Mock()
    monkeypatch.setattr(automations, "on_invoice_paid", paid)
    assert stripe_checkout.prepare_manual_payment(invoice_id) == ("paid", "cs_synthetic")
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "paid"
    paid.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/stripe/webhook", "/stripe/platform/webhook"])
async def test_signed_real_event_pays_once_and_rejects_bad_signature(sdk_env, monkeypatch, path):
    invoice_id = _invoice()
    event = _event(_session(invoice_id, status="complete", payment_status="paid"))
    paid = Mock()
    monkeypatch.setattr(automations, "on_invoice_paid", paid)
    invalid = await _post_event(sdk_env, path, event, valid=False)
    assert invalid.status_code == 400
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "sent"
    for _ in range(2):
        response = await _post_event(sdk_env, path, event)
        assert response.status_code == 200
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "paid"
    paid.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["amount", "tenant", "destination"])
async def test_signed_resource_mismatch_never_pays(sdk_env, monkeypatch, changed):
    invoice_id = _invoice()
    session = _session(invoice_id, status="complete", payment_status="paid")
    if changed == "amount":
        session.amount_total = 4999
    elif changed == "tenant":
        session.metadata.studio_id = "other-studio"
    else:
        session.metadata.stripe_destination_account = "acct_other"
    paid = Mock()
    monkeypatch.setattr(automations, "on_invoice_paid", paid)
    response = await _post_event(sdk_env, "/stripe/webhook", _event(session))
    assert response.status_code == 200
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "sent"
    assert db.one("SELECT status FROM stripe_event_receipts")["status"] == "rejected"
    paid.assert_not_called()


def _deposit(invoice_id):
    cutoff = dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(minutes=20)
    listing = db.one("SELECT listing_id FROM invoices WHERE id=?", (invoice_id,))["listing_id"]
    inquiry = db.run(
        """INSERT INTO inquiries
           (studio_id,name,email,status,listing_id,invoice_id,payment_expires_at)
           VALUES ('default','Synthetic','synthetic@example.invalid','pending_payment',?,?,?)""",
        (listing, invoice_id, cutoff.strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.run(
        "UPDATE invoices SET invoice_kind='deposit',inquiry_id=? WHERE id=?", (inquiry, invoice_id)
    )
    return inquiry, cutoff


@pytest.mark.asyncio
async def test_signed_real_event_preserves_late_payment_rejection(sdk_env, monkeypatch):
    invoice_id = _invoice()
    _, cutoff = _deposit(invoice_id)
    paid = Mock()
    monkeypatch.setattr(automations, "on_deposit_paid", paid)
    event = _event(
        _session(invoice_id, status="complete", payment_status="paid"),
        created=int(cutoff.timestamp()) + 1,
    )
    response = await _post_event(sdk_env, "/stripe/webhook", event)
    assert response.status_code == 200
    assert db.one("SELECT error FROM stripe_event_receipts")["error"] == "payment_hold_expired"
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "sent"
    paid.assert_not_called()


def test_commerce_paid_recovery_accepts_real_resource(sdk_env, monkeypatch):
    invoice_id = _invoice()
    inquiry, cutoff = _deposit(invoice_id)
    session = _session(
        invoice_id, status="complete", payment_status="paid", expires_at=int(cutoff.timestamp())
    )
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", Mock(return_value=session))

    # Observe reconciliation without invoking unrelated booking workflow effects.
    def confirm(inquiry_id, listing_id, **kwargs):
        assert kwargs["payment_event_created"] == cutoff.timestamp()
        db.run(
            "UPDATE inquiries SET status='confirmed' WHERE id=? AND studio_id='default'",
            (inquiry_id,),
        )
        return True

    monkeypatch.setattr(automations, "on_deposit_paid", confirm)
    assert commerce.expire_pending_bookings() == 0
    assert db.one("SELECT status FROM inquiries WHERE id=?", (inquiry,))["status"] == "confirmed"
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "paid"


def _pending_subscription():
    db.run("""UPDATE studio SET stripe_customer_id='cus_synthetic',
              platform_checkout_session_id='cs_subscription',platform_checkout_plan='pro',
              platform_checkout_url='https://checkout.example.invalid/subscription',
              billing_status='trialing' WHERE id='default'""")
    return stripe.checkout.Session.construct_from(
        {
            "object": "checkout.session",
            "id": "cs_subscription",
            "mode": "subscription",
            "customer": "cus_synthetic",
            "subscription": "sub_synthetic",
            "status": "open",
            "url": "https://checkout.example.invalid/subscription",
            "metadata": {"studio_id": "default", "plan_tier": "pro"},
        },
        KEY,
    )


def test_platform_checkout_reuses_real_resource(sdk_env, monkeypatch):
    session = _pending_subscription()
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", Mock(return_value=session))
    assert platform_billing.create_checkout("pro") == session.url


@pytest.mark.asyncio
async def test_signed_subscription_event_handles_nested_metadata(sdk_env):
    session = _pending_subscription()
    for _ in range(2):
        response = await _post_event(sdk_env, "/stripe/platform/webhook", _event(session))
        assert response.status_code == 200
    row = db.one(
        "SELECT billing_status,plan_tier,stripe_subscription_id FROM studio WHERE id='default'"
    )
    assert tuple(row) == ("active", "pro", "sub_synthetic")


@pytest.mark.asyncio
async def test_signed_account_event_updates_only_bound_studio(sdk_env):
    db.run("UPDATE studio SET stripe_connect_account_id='acct_synthetic' WHERE id='default'")
    db.run(
        "INSERT INTO studio (id,name,slug,stripe_connect_account_id) VALUES ('other','Other','other','acct_other')"
    )
    account = stripe.Account.construct_from(
        {
            "object": "account",
            "id": "acct_synthetic",
            "charges_enabled": True,
            "payouts_enabled": True,
        },
        KEY,
    )
    response = await _post_event(
        sdk_env, "/stripe/platform/webhook", _event(account, kind="account.updated")
    )
    assert response.status_code == 200
    assert db.one("SELECT stripe_connect_charges_enabled FROM studio WHERE id='default'")[0] == 1
    assert db.one("SELECT stripe_connect_charges_enabled FROM studio WHERE id='other'")[0] == 0


def test_direct_completed_resource_preserves_recursive_metadata(sdk_env, monkeypatch):
    invoice_id = _invoice()
    monkeypatch.setattr(upsell, "mark_paid", Mock())
    monkeypatch.setattr(automations, "on_invoice_paid", Mock())
    assert stripe_webhooks.handle_invoice_checkout_completed(
        _session(invoice_id, payment_status="paid"),
        event_id="evt_direct",
        event_created=time.time(),
    )
