"""Phase 19 — Stripe test-mode dogfood (Connect client payments + platform billing webhooks)."""

import importlib
from unittest.mock import patch

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.main as main
import eos.platform_billing as platform_billing
import eos.stripe_connect as stripe_connect
import eos.stripe_webhooks as stripe_webhooks
import eos.tenant as tenant
import eos.users as users
import pytest


@pytest.fixture()
def saas_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_SAAS_MODE", "true")
    monkeypatch.setenv("EOS_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("EOS_BASE_DOMAIN", "eos.test")
    monkeypatch.setenv("EOS_STRIPE_PLATFORM_SECRET_KEY", "sk_test_platform")
    monkeypatch.setenv("EOS_STRIPE_PLATFORM_WEBHOOK_SECRET", "whsec_test")
    for mod in (
        config,
        db,
        jobs,
        tenant,
        users,
        stripe_connect,
        stripe_webhooks,
        platform_billing,
        main,
    ):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    yield main.app
    jobs.stop()


def _seed_studio(slug: str) -> None:
    db.run(
        """INSERT INTO studio (id, name, slug, contact_email, saas_enabled, plan_tier, billing_status, active,
           stripe_connect_account_id, stripe_connect_charges_enabled)
           VALUES (?,?,?,?,1,'trial','trialing',1,'acct_test',1)""",
        (slug, slug.title(), slug, f"owner@{slug}.test"),
    )
    users.create_user(
        f"owner@{slug}.test", "secret-pass-1", name="Owner", role="owner", studio_id=slug
    )


def _seed_invoice(studio_id: str, *, kind: str = "full") -> int:
    lid = db.run(
        "INSERT INTO listings (studio_id, title) VALUES (?, '123 Main')",
        (studio_id,),
    )
    return db.run(
        """INSERT INTO invoices (studio_id, listing_id, slug, title, amount_cents, status, line_items, invoice_kind)
           VALUES (?, ?, 'inv-test', 'Shoot fee', 50000, 'sent', '[]', ?)""",
        (studio_id, lid, kind),
    )


def test_connect_client_payment_webhook_marks_invoice_paid(saas_env):
    _seed_studio("pay-webhook")
    iid = _seed_invoice("pay-webhook")
    session = {
        "id": "cs_test_1",
        "mode": "payment",
        "metadata": {"invoice_id": str(iid)},
    }
    event = {"type": "checkout.session.completed", "data": {"object": session}}

    tenant.set_studio("default")
    platform_billing.handle_webhook_event(event)

    row = db.one("SELECT status FROM invoices WHERE id=?", (iid,))
    assert row["status"] == "paid"
    assert tenant.get_studio_id() == "default"


def test_connect_client_payment_binds_invoice_studio_for_automations(saas_env):
    _seed_studio("pay-bind")
    iid = _seed_invoice("pay-bind")
    listing_id = db.one("SELECT listing_id FROM invoices WHERE id=?", (iid,))["listing_id"]
    session = {"id": "cs_test_bind", "mode": "payment", "metadata": {"invoice_id": str(iid)}}
    seen_studios: list[str] = []

    def capture_invoice_paid(_listing_id: int) -> None:
        assert _listing_id == listing_id
        seen_studios.append(tenant.get_studio_id())

    tenant.set_studio("default")
    with patch("eos.stripe_webhooks.automations.on_invoice_paid", side_effect=capture_invoice_paid):
        assert stripe_webhooks.handle_invoice_checkout_completed(session) is True

    row = db.one("SELECT status FROM invoices WHERE id=?", (iid,))
    assert row["status"] == "paid"
    assert seen_studios == ["pay-bind"]
    assert tenant.get_studio_id() == "default"


def test_connect_client_payment_idempotent(saas_env):
    _seed_studio("pay-idem")
    iid = _seed_invoice("pay-idem")
    session = {"id": "cs_test_2", "mode": "payment", "metadata": {"invoice_id": str(iid)}}
    tenant.set_studio("default")
    stripe_webhooks.handle_invoice_checkout_completed(session)
    stripe_webhooks.handle_invoice_checkout_completed(session)
    row = db.one("SELECT status FROM invoices WHERE id=?", (iid,))
    assert row["status"] == "paid"
    assert tenant.get_studio_id() == "default"


def test_subscription_webhook_still_works(saas_env):
    _seed_studio("sub-webhook")
    db.run("UPDATE studio SET stripe_customer_id='cus_test' WHERE id=?", ("sub-webhook",))
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "mode": "subscription",
                "subscription": "sub_123",
                "metadata": {"studio_id": "sub-webhook", "plan_tier": "starter"},
            }
        },
    }
    tenant.set_studio("default")
    platform_billing.handle_webhook_event(event)
    row = db.one(
        "SELECT billing_status, plan_tier, stripe_subscription_id FROM studio WHERE id=?",
        ("sub-webhook",),
    )
    assert row["billing_status"] == "active"
    assert row["plan_tier"] == "starter"
    assert row["stripe_subscription_id"] == "sub_123"
    assert tenant.get_studio_id() == "default"
