"""Payment integrity: signed Stripe events, exact bindings, and replay safety."""

from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import eos.automations as automations
import eos.config as config
import eos.db as db
import eos.routes.pay as pay_routes
import eos.stripe_checkout as stripe_checkout
import eos.stripe_connect as stripe_connect
import eos.stripe_webhooks as stripe_webhooks
import eos.tenant as tenant
import eos.upsell as upsell
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def payment_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_BASE_URL", "http://testserver")
    monkeypatch.setenv("EOS_BASE_DOMAIN", "eos.test")
    monkeypatch.setenv("EOS_SAAS_MODE", "true")
    monkeypatch.setenv("EOS_STRIPE_SECRET_KEY", "")
    monkeypatch.setenv("EOS_STRIPE_WEBHOOK_SECRET", "whsec_invoice_test")
    monkeypatch.setenv("EOS_STRIPE_PLATFORM_SECRET_KEY", "sk_test_platform")

    for module in (
        config,
        db,
        tenant,
        automations,
        upsell,
        stripe_connect,
        stripe_checkout,
        stripe_webhooks,
        pay_routes,
    ):
        importlib.reload(module)

    config.ensure_dirs()
    db.migrate()
    tenant.set_studio("default")
    yield
    tenant.set_studio("default")


def _seed_invoice(
    *,
    studio_id: str = "payment-studio",
    session_id: str | None = "cs_bound",
    destination: str | None = "acct_bound",
) -> tuple[int, int]:
    db.run(
        "INSERT INTO studio (id, name, slug) VALUES (?,?,?)",
        (studio_id, "Payment Studio", studio_id),
    )
    listing_id = db.run(
        "INSERT INTO listings (studio_id, title) VALUES (?, '123 Main')",
        (studio_id,),
    )
    invoice_id = db.run(
        """INSERT INTO invoices
           (studio_id, listing_id, slug, title, amount_cents, status, line_items,
            invoice_kind, currency, stripe_session_id, stripe_destination_account)
           VALUES (?,?,?, 'Shoot fee', 50000, 'sent', '[]', 'full', 'usd', ?, ?)""",
        (studio_id, listing_id, f"invoice-{studio_id}", session_id, destination),
    )
    return invoice_id, listing_id


def _event(
    invoice_id: int,
    *,
    event_id: str = "evt_invoice_paid",
    session_id: str = "cs_bound",
    destination: str = "acct_bound",
    studio_id: str = "payment-studio",
) -> dict:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 50000,
                "currency": "usd",
                "metadata": {
                    "invoice_id": str(invoice_id),
                    "studio_id": studio_id,
                    "payment_rail": "connect",
                    "stripe_destination_account": destination,
                    "currency": "usd",
                },
            }
        },
    }


def _effect_mocks(monkeypatch) -> tuple[Mock, Mock]:
    upsell_paid = Mock()
    invoice_paid = Mock()
    monkeypatch.setattr(stripe_webhooks.upsell, "mark_paid", upsell_paid)
    monkeypatch.setattr(stripe_webhooks.automations, "on_invoice_paid", invoice_paid)
    return upsell_paid, invoice_paid


def test_valid_event_marks_exact_invoice_and_replay_runs_effects_once(payment_env, monkeypatch):
    invoice_id, listing_id = _seed_invoice()
    event = _event(invoice_id)
    upsell_paid, invoice_paid = _effect_mocks(monkeypatch)

    assert stripe_webhooks.handle_invoice_checkout_event(event) is True
    assert stripe_webhooks.handle_invoice_checkout_event(event) is False

    invoice = db.one("SELECT status, paid_at FROM invoices WHERE id=?", (invoice_id,))
    receipt = db.one(
        """SELECT event_id, event_type, status, attempts, studio_id
           FROM stripe_event_receipts WHERE source='stripe' AND event_id=?""",
        (event["id"],),
    )
    assert invoice["status"] == "paid"
    assert invoice["paid_at"] is not None
    assert dict(receipt) == {
        "event_id": event["id"],
        "event_type": "checkout.session.completed",
        "status": "processed",
        "attempts": 2,
        "studio_id": "payment-studio",
    }
    upsell_paid.assert_called_once_with(invoice_id)
    invoice_paid.assert_called_once_with(listing_id, invoice_id=invoice_id)
    assert tenant.get_studio_id() == "default"


@pytest.mark.parametrize(
    ("path", "bad_value", "expected_error"),
    [
        ("id", "cs_wrong", "stripe_session_mismatch"),
        ("amount_total", 49999, "amount_mismatch"),
        ("currency", "eur", "currency_mismatch"),
        ("metadata.currency", "eur", "metadata_currency_mismatch"),
        ("metadata.stripe_destination_account", "acct_wrong", "destination_mismatch"),
        ("metadata.studio_id", "other-studio", "studio_mismatch"),
        ("metadata.payment_rail", "legacy", "payment_rail_mismatch"),
        ("payment_status", "unpaid", "checkout_not_paid"),
    ],
)
def test_mismatched_payment_is_rejected_without_invoice_or_workflow_mutation(
    payment_env,
    monkeypatch,
    path,
    bad_value,
    expected_error,
):
    invoice_id, listing_id = _seed_invoice()
    event = deepcopy(_event(invoice_id, event_id=f"evt_{expected_error}"))
    session = event["data"]["object"]
    if path.startswith("metadata."):
        session["metadata"][path.removeprefix("metadata.")] = bad_value
    else:
        session[path] = bad_value
    upsell_paid, invoice_paid = _effect_mocks(monkeypatch)

    assert stripe_webhooks.handle_invoice_checkout_event(event) is False

    invoice = db.one("SELECT status, paid_at FROM invoices WHERE id=?", (invoice_id,))
    listing = db.one("SELECT status FROM listings WHERE id=?", (listing_id,))
    receipt = db.one(
        """SELECT status, attempts, error FROM stripe_event_receipts
           WHERE source='stripe' AND event_id=?""",
        (event["id"],),
    )
    assert dict(invoice) == {"status": "sent", "paid_at": None}
    assert listing["status"] == "lead"
    assert dict(receipt) == {
        "status": "rejected",
        "attempts": 1,
        "error": expected_error,
    }
    upsell_paid.assert_not_called()
    invoice_paid.assert_not_called()


def test_concurrent_distinct_events_compare_and_set_effects_once(payment_env, monkeypatch):
    invoice_id, _listing_id = _seed_invoice()
    events = [
        _event(invoice_id, event_id="evt_concurrent_1"),
        _event(invoice_id, event_id="evt_concurrent_2"),
    ]
    upsell_paid, invoice_paid = _effect_mocks(monkeypatch)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(stripe_webhooks.handle_invoice_checkout_event, events))

    assert sorted(results) == [False, True]
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "paid"
    assert (
        db.one("SELECT COUNT(*) AS n FROM stripe_event_receipts WHERE status='processed'")["n"] == 2
    )
    assert (
        db.one("SELECT COUNT(*) AS n FROM stripe_event_receipts WHERE status='rejected'")["n"] == 0
    )
    upsell_paid.assert_called_once_with(invoice_id)
    assert invoice_paid.call_count == 1


@pytest.mark.asyncio
async def test_webhook_route_requires_signature_and_passes_verified_full_event(
    payment_env,
    monkeypatch,
):
    app = FastAPI()
    app.include_router(pay_routes.router)
    construct_event = Mock()
    handled = Mock(return_value=True)
    monkeypatch.setattr(pay_routes.stripe.Webhook, "construct_event", construct_event)
    monkeypatch.setattr(
        pay_routes.stripe_webhooks,
        "handle_invoice_checkout_event",
        handled,
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.post("/stripe/webhook", content=b"{}")
        assert missing.status_code == 400
        construct_event.assert_not_called()

        construct_event.side_effect = ValueError("bad signature")
        invalid = await client.post(
            "/stripe/webhook",
            content=b"{}",
            headers={"stripe-signature": "bad"},
        )
        assert invalid.status_code == 400
        handled.assert_not_called()

        event = {
            "id": "evt_route_verified",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_route"}},
        }
        construct_event.side_effect = None
        construct_event.return_value = event
        valid = await client.post(
            "/stripe/webhook",
            content=b'{"verified":true}',
            headers={"stripe-signature": "valid"},
        )

    assert valid.status_code == 200
    assert valid.json() == {"ok": True}
    handled.assert_called_once_with(event)
    construct_event.assert_called_with(
        b'{"verified":true}',
        "valid",
        config.STRIPE_WEBHOOK_SECRET,
    )


def test_connect_checkout_binds_metadata_destination_and_invoice(payment_env, monkeypatch):
    invoice_id, _listing_id = _seed_invoice(session_id=None, destination=None)
    tenant.set_studio("payment-studio")
    created = SimpleNamespace(id="cs_new", url="https://checkout.test/new")
    create = Mock(return_value=created)
    monkeypatch.setattr(stripe_checkout, "payment_rail", lambda: ("connect", "acct_new"))
    monkeypatch.setattr(stripe_checkout.stripe_connect, "application_fee_cents", lambda _n: 250)
    monkeypatch.setattr(stripe_checkout.stripe_connect, "platform_api_key", lambda: "sk_test")
    monkeypatch.setattr(stripe_checkout.stripe.checkout.Session, "create", create)

    result = stripe_checkout.create_payment_session(
        amount_cents=50000,
        title="Shoot fee",
        success_url="https://studio.test/success",
        cancel_url="https://studio.test/cancel",
        customer_email=None,
        metadata={"invoice_id": str(invoice_id), "studio_id": "spoofed"},
        currency="usd",
    )

    assert result is created
    kwargs = create.call_args.kwargs
    assert kwargs["metadata"] == {
        "invoice_id": str(invoice_id),
        "studio_id": "payment-studio",
        "payment_rail": "connect",
        "stripe_destination_account": "acct_new",
        "currency": "usd",
    }
    assert kwargs["payment_intent_data"] == {
        "transfer_data": {"destination": "acct_new"},
        "application_fee_amount": 250,
    }
    assert kwargs["line_items"][0]["price_data"]["currency"] == "usd"
    invoice = db.one(
        """SELECT stripe_session_id, stripe_destination_account, currency
           FROM invoices WHERE id=?""",
        (invoice_id,),
    )
    assert dict(invoice) == {
        "stripe_session_id": "cs_new",
        "stripe_destination_account": "acct_new",
        "currency": "usd",
    }


def test_saas_studio_never_falls_back_to_legacy_platform_key(payment_env, monkeypatch):
    _seed_invoice()
    tenant.set_studio("payment-studio")
    monkeypatch.setattr(config, "SAAS_MODE", True)
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_legacy")
    monkeypatch.setattr(stripe_checkout.stripe_connect, "studio_connect", lambda: None)
    create = Mock()
    monkeypatch.setattr(stripe_checkout.stripe.checkout.Session, "create", create)

    assert stripe_checkout.payment_rail() == ("unavailable", None)
    assert stripe_checkout.payments_configured() is False
    with pytest.raises(HTTPException) as exc_info:
        stripe_checkout.create_payment_session(
            amount_cents=50000,
            title="Shoot fee",
            success_url="https://studio.test/success",
            cancel_url="https://studio.test/cancel",
            customer_email=None,
            metadata={"invoice_id": "1"},
        )
    assert exc_info.value.status_code == 503
    create.assert_not_called()


def test_effect_failure_rolls_back_payment_and_replay_recovers(payment_env, monkeypatch):
    invoice_id, _listing_id = _seed_invoice()
    event = _event(invoice_id, event_id="evt_effect_retry")
    calls = {"count": 0}

    def flaky_effect(_listing_id, *, invoice_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError(f"effect failed for {invoice_id}")

    monkeypatch.setattr(stripe_webhooks.automations, "on_invoice_paid", flaky_effect)
    with pytest.raises(RuntimeError, match="effect failed"):
        stripe_webhooks.handle_invoice_checkout_event(event)

    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "sent"
    receipt = db.one(
        "SELECT status, attempts FROM stripe_event_receipts WHERE event_id=?", (event["id"],)
    )
    assert dict(receipt) == {"status": "failed", "attempts": 1}

    assert stripe_webhooks.handle_invoice_checkout_event(event) is True
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "paid"
    receipt = db.one(
        "SELECT status, attempts FROM stripe_event_receipts WHERE event_id=?", (event["id"],)
    )
    assert dict(receipt) == {"status": "processed", "attempts": 2}


def test_prepare_manual_payment_expires_exact_open_checkout(payment_env, monkeypatch):
    invoice_id, _listing_id = _seed_invoice()
    tenant.set_studio("payment-studio")
    open_session = deepcopy(_event(invoice_id)["data"]["object"])
    open_session.update(status="open", payment_status="unpaid")
    expired_session = {**open_session, "status": "expired"}
    retrieve = Mock(return_value=open_session)
    expire = Mock(return_value=expired_session)
    monkeypatch.setattr(stripe_checkout, "retrieve_payment_session", retrieve)
    monkeypatch.setattr(stripe_checkout, "expire_payment_session", expire)

    assert stripe_checkout.prepare_manual_payment(invoice_id) == ("unpaid", "cs_bound")

    retrieve.assert_called_once_with("cs_bound", destination="acct_bound")
    expire.assert_called_once_with("cs_bound", destination="acct_bound")
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "sent"


def test_prepare_manual_payment_reconciles_provider_paid_once(payment_env, monkeypatch):
    invoice_id, listing_id = _seed_invoice()
    tenant.set_studio("payment-studio")
    paid_session = {
        **deepcopy(_event(invoice_id)["data"]["object"]),
        "status": "complete",
    }
    retrieve = Mock(return_value=paid_session)
    expire = Mock(side_effect=AssertionError("paid Checkout must not be expired"))
    monkeypatch.setattr(stripe_checkout, "retrieve_payment_session", retrieve)
    monkeypatch.setattr(stripe_checkout, "expire_payment_session", expire)
    upsell_paid, invoice_paid = _effect_mocks(monkeypatch)

    assert stripe_checkout.prepare_manual_payment(invoice_id) == ("paid", "cs_bound")

    invoice = db.one("SELECT status, paid_at FROM invoices WHERE id=?", (invoice_id,))
    receipt = db.one(
        """SELECT source, event_id, status, attempts, studio_id
           FROM stripe_event_receipts
           WHERE source='stripe-manual-reconcile'""",
    )
    assert invoice["status"] == "paid"
    assert invoice["paid_at"] is not None
    assert dict(receipt) == {
        "source": "stripe-manual-reconcile",
        "event_id": "manual-reconcile:cs_bound",
        "status": "processed",
        "attempts": 1,
        "studio_id": "payment-studio",
    }
    upsell_paid.assert_called_once_with(invoice_id)
    invoice_paid.assert_called_once_with(listing_id, invoice_id=invoice_id)
    expire.assert_not_called()

    with pytest.raises(HTTPException, match="duplicate charge") as exc_info:
        stripe_checkout.prepare_manual_payment(invoice_id)
    assert exc_info.value.status_code == 409
    upsell_paid.assert_called_once_with(invoice_id)
    invoice_paid.assert_called_once_with(listing_id, invoice_id=invoice_id)


def test_prepare_manual_payment_blocks_claim_without_session(payment_env, monkeypatch):
    invoice_id, _listing_id = _seed_invoice(session_id=None, destination=None)
    tenant.set_studio("payment-studio")
    db.run(
        """UPDATE invoices SET stripe_checkout_claimed_at=datetime('now')
           WHERE id=?""",
        (invoice_id,),
    )
    retrieve = Mock(side_effect=AssertionError("no Checkout session exists"))
    expire = Mock(side_effect=AssertionError("no Checkout session exists"))
    monkeypatch.setattr(stripe_checkout, "retrieve_payment_session", retrieve)
    monkeypatch.setattr(stripe_checkout, "expire_payment_session", expire)

    with pytest.raises(HTTPException, match="creation is unresolved") as exc_info:
        stripe_checkout.prepare_manual_payment(invoice_id)

    assert exc_info.value.status_code == 409
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "sent"
    retrieve.assert_not_called()
    expire.assert_not_called()


def test_prepare_manual_payment_fails_closed_when_retrieve_fails(payment_env, monkeypatch):
    invoice_id, _listing_id = _seed_invoice()
    tenant.set_studio("payment-studio")
    retrieve = Mock(side_effect=RuntimeError("provider unavailable"))
    expire = Mock(side_effect=AssertionError("unverified Checkout must not be expired"))
    monkeypatch.setattr(stripe_checkout, "retrieve_payment_session", retrieve)
    monkeypatch.setattr(stripe_checkout, "expire_payment_session", expire)

    with pytest.raises(HTTPException, match="Unable to verify Checkout") as exc_info:
        stripe_checkout.prepare_manual_payment(invoice_id)

    assert exc_info.value.status_code == 503
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "sent"
    retrieve.assert_called_once_with("cs_bound", destination="acct_bound")
    expire.assert_not_called()


def test_prepare_manual_payment_expires_persisted_open_session_for_local_paid_invoice(
    payment_env,
    monkeypatch,
):
    invoice_id, _listing_id = _seed_invoice()
    tenant.set_studio("payment-studio")
    db.run(
        "UPDATE invoices SET status='paid', paid_at=datetime('now') WHERE id=?",
        (invoice_id,),
    )
    open_session = deepcopy(_event(invoice_id)["data"]["object"])
    open_session.update(status="open", payment_status="unpaid")
    expired_session = {**open_session, "status": "expired"}
    retrieve = Mock(return_value=open_session)
    expire = Mock(return_value=expired_session)
    monkeypatch.setattr(stripe_checkout, "retrieve_payment_session", retrieve)
    monkeypatch.setattr(stripe_checkout, "expire_payment_session", expire)
    upsell_paid, invoice_paid = _effect_mocks(monkeypatch)

    assert stripe_checkout.prepare_manual_payment(invoice_id) == ("paid", "cs_bound")

    retrieve.assert_called_once_with("cs_bound", destination="acct_bound")
    expire.assert_called_once_with("cs_bound", destination="acct_bound")
    assert db.one("SELECT status FROM invoices WHERE id=?", (invoice_id,))["status"] == "paid"
    upsell_paid.assert_not_called()
    invoice_paid.assert_not_called()


@pytest.mark.asyncio
async def test_public_pay_replay_never_replaces_completed_provider_checkout(
    payment_env,
    monkeypatch,
):
    invoice_id, _listing_id = _seed_invoice(session_id=None, destination=None)
    tenant.set_studio("payment-studio")
    created = SimpleNamespace(id="cs_public_replay", url="https://checkout.test/first")
    create = Mock(return_value=created)
    retrieve = Mock()
    monkeypatch.setattr(stripe_checkout, "payment_rail", lambda: ("connect", "acct_replay"))
    monkeypatch.setattr(stripe_checkout.stripe_connect, "application_fee_cents", lambda _n: 0)
    monkeypatch.setattr(stripe_checkout.stripe_connect, "platform_api_key", lambda: "sk_test")
    monkeypatch.setattr(stripe_checkout.stripe.checkout.Session, "create", create)
    monkeypatch.setattr(stripe_checkout.stripe.checkout.Session, "retrieve", retrieve)

    first = await pay_routes.pay_invoice("invoice-payment-studio")
    assert first.status_code == 303
    assert first.headers["location"] == "https://checkout.test/first"

    paid_session = deepcopy(
        _event(
            invoice_id,
            session_id="cs_public_replay",
            destination="acct_replay",
        )["data"]["object"]
    )
    paid_session.update(status="complete", url=None)
    retrieve.return_value = paid_session

    with pytest.raises(HTTPException, match="already paid") as exc_info:
        await pay_routes.pay_invoice("invoice-payment-studio")

    assert exc_info.value.status_code == 409
    assert create.call_count == 1
    retrieve.assert_called_once_with("cs_public_replay", api_key="sk_test")
    invoice = db.one(
        "SELECT status, stripe_session_id FROM invoices WHERE id=?",
        (invoice_id,),
    )
    assert dict(invoice) == {
        "status": "sent",
        "stripe_session_id": "cs_public_replay",
    }


def test_expired_unpaid_checkout_is_the_only_replaceable_provider_state(
    payment_env,
    monkeypatch,
):
    invoice_id, _listing_id = _seed_invoice()
    tenant.set_studio("payment-studio")
    expired = deepcopy(_event(invoice_id)["data"]["object"])
    expired.update(status="expired", payment_status="unpaid", url=None)
    replacement = SimpleNamespace(id="cs_replacement", url="https://checkout.test/replacement")
    retrieve = Mock(return_value=expired)
    create = Mock(return_value=replacement)
    monkeypatch.setattr(stripe_checkout, "payment_rail", lambda: ("connect", "acct_bound"))
    monkeypatch.setattr(stripe_checkout.stripe_connect, "application_fee_cents", lambda _n: 0)
    monkeypatch.setattr(stripe_checkout.stripe_connect, "platform_api_key", lambda: "sk_test")
    monkeypatch.setattr(stripe_checkout.stripe.checkout.Session, "retrieve", retrieve)
    monkeypatch.setattr(stripe_checkout.stripe.checkout.Session, "create", create)

    result = stripe_checkout.create_payment_session(
        amount_cents=50000,
        title="Shoot fee",
        success_url="https://studio.test/success",
        cancel_url="https://studio.test/cancel",
        customer_email=None,
        metadata={"invoice_id": str(invoice_id)},
        existing_session_id="cs_bound",
        currency="usd",
    )

    assert result is replacement
    retrieve.assert_called_once_with("cs_bound", api_key="sk_test")
    create.assert_called_once()
    assert (
        db.one(
            "SELECT stripe_session_id FROM invoices WHERE id=?",
            (invoice_id,),
        )["stripe_session_id"]
        == "cs_replacement"
    )
