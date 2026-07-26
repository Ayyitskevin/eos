"""Focused integrity tests for SaaS platform-subscription billing."""

from __future__ import annotations

import datetime as dt
import importlib

import eos.billing_gate as billing_gate
import eos.config as config
import eos.db as db
import eos.plan_limits as plan_limits
import eos.platform_billing as platform_billing
import eos.tenant as tenant
import pytest
from starlette.requests import Request


class _StripeObject(dict):
    """Small Stripe-object stand-in supporting mapping and attribute access."""

    __getattr__ = dict.__getitem__


@pytest.fixture()
def billing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_BASE_URL", "https://eos.test")
    monkeypatch.setenv("EOS_STRIPE_PLATFORM_SECRET_KEY", "sk_test_platform")
    monkeypatch.setenv("EOS_STRIPE_PRICE_STARTER", "price_starter")
    monkeypatch.setenv("EOS_STRIPE_PRICE_PRO", "price_pro")
    for module in (config, db, tenant, platform_billing):
        importlib.reload(module)
    config.ensure_dirs()
    db.migrate()
    tenant.set_studio("default")
    db.run(
        """UPDATE studio
           SET name='Test Studio', slug='test-studio',
               contact_email='owner@example.test'
           WHERE id='default'"""
    )
    yield
    tenant.set_studio("default")


def _studio():
    return db.one("SELECT * FROM studio WHERE id='default'")


def _seed_pending_checkout(
    *,
    session_id: str = "cs_pending",
    customer_id: str = "cus_bound",
    plan: str = "pro",
    url: str = "https://checkout.test/pending",
    billing_status: str = "trialing",
    subscription_id: str = "",
) -> None:
    db.run(
        """UPDATE studio
           SET stripe_customer_id=?, stripe_subscription_id=?,
               billing_status=?, platform_checkout_session_id=?,
               platform_checkout_plan=?, platform_checkout_url=?
           WHERE id='default'""",
        (
            customer_id,
            subscription_id,
            billing_status,
            session_id,
            plan,
            url,
        ),
    )


def _completed_event(
    *,
    session_id: str = "cs_pending",
    customer_id: str = "cus_bound",
    subscription_id: str = "sub_new",
    plan: str = "pro",
    event_id: str = "evt_subscription",
    created: int = 1_800_000_000,
) -> dict:
    return {
        "id": event_id,
        "created": created,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "mode": "subscription",
                "customer": customer_id,
                "subscription": subscription_id,
                "metadata": {"studio_id": "default", "plan_tier": plan},
            }
        },
    }


def _subscription_event(
    *, event_id: str, created: int, status: str, plan_tier: str = "pro"
) -> dict:
    return {
        "id": event_id,
        "created": created,
        "type": (
            "customer.subscription.deleted"
            if status == "canceled"
            else "customer.subscription.updated"
        ),
        "data": {
            "object": {
                "id": "sub_bound",
                "customer": "cus_bound",
                "status": status,
                "metadata": {"studio_id": "default", "plan_tier": plan_tier},
            }
        },
    }


def test_customer_creation_uses_deterministic_key_and_cas_winner(billing_env, monkeypatch):
    calls: list[dict] = []

    def create_customer(**kwargs):
        calls.append(kwargs)
        # Model another request winning persistence while this provider call runs.
        db.run("UPDATE studio SET stripe_customer_id='cus_winner' WHERE id='default'")
        return _StripeObject(id="cus_loser")

    monkeypatch.setattr(platform_billing.stripe.Customer, "create", create_customer)

    assert platform_billing.ensure_customer() == "cus_winner"
    assert _studio()["stripe_customer_id"] == "cus_winner"
    assert len(calls) == 1
    assert calls[0]["idempotency_key"] == "eos-platform-customer:default"
    assert calls[0]["metadata"]["studio_id"] == "default"


def test_same_plan_checkout_replay_returns_stored_open_session(billing_env, monkeypatch):
    customer_calls: list[dict] = []
    checkout_calls: list[dict] = []
    retrieve_calls: list[tuple] = []

    def create_customer(**kwargs):
        customer_calls.append(kwargs)
        return _StripeObject(id="cus_1")

    def create_checkout(**kwargs):
        checkout_calls.append(kwargs)
        return _StripeObject(id="cs_1", url="https://checkout.test/cs_1")

    def retrieve_checkout(*args, **kwargs):
        retrieve_calls.append((args, kwargs))
        return _StripeObject(
            id="cs_1",
            url="https://checkout.test/cs_1",
            status="open",
            mode="subscription",
            customer="cus_1",
            metadata={"studio_id": "default", "plan_tier": "pro"},
        )

    monkeypatch.setattr(platform_billing.stripe.Customer, "create", create_customer)
    monkeypatch.setattr(platform_billing.stripe.checkout.Session, "create", create_checkout)
    monkeypatch.setattr(platform_billing.stripe.checkout.Session, "retrieve", retrieve_checkout)

    assert platform_billing.create_checkout("pro") == "https://checkout.test/cs_1"
    assert platform_billing.create_checkout("pro") == "https://checkout.test/cs_1"

    row = _studio()
    assert row["stripe_customer_id"] == "cus_1"
    assert row["platform_checkout_session_id"] == "cs_1"
    assert row["platform_checkout_plan"] == "pro"
    assert row["platform_checkout_url"] == "https://checkout.test/cs_1"
    assert len(customer_calls) == 1
    assert customer_calls[0]["idempotency_key"] == "eos-platform-customer:default"
    assert len(checkout_calls) == 1
    assert checkout_calls[0]["idempotency_key"] == "eos-platform-subscription:default:initial"
    assert checkout_calls[0]["customer"] == "cus_1"
    assert checkout_calls[0]["line_items"] == [{"price": "price_pro", "quantity": 1}]
    assert retrieve_calls == [(("cs_1",), {"api_key": "sk_test_platform"})]


def test_active_subscription_redirects_to_portal_without_checkout(billing_env, monkeypatch):
    db.run(
        """UPDATE studio
           SET billing_status='active', plan_tier='pro',
               stripe_customer_id='cus_active',
               stripe_subscription_id='sub_active'
           WHERE id='default'"""
    )
    portal_calls: list[dict] = []

    def create_portal(**kwargs):
        portal_calls.append(kwargs)
        return _StripeObject(url="https://billing.test/portal")

    monkeypatch.setattr(platform_billing.stripe.billing_portal.Session, "create", create_portal)

    def unexpected_checkout(**_kwargs):
        raise AssertionError("active subscriptions must not create Checkout sessions")

    monkeypatch.setattr(platform_billing.stripe.checkout.Session, "create", unexpected_checkout)
    monkeypatch.setattr(platform_billing.stripe.Customer, "create", unexpected_checkout)

    assert platform_billing.create_checkout("starter") == "https://billing.test/portal"
    assert portal_calls == [
        {
            "api_key": "sk_test_platform",
            "customer": "cus_active",
            "return_url": "https://eos.test/admin/billing",
        }
    ]


def test_pending_checkout_retrieval_failure_fails_closed(billing_env, monkeypatch):
    _seed_pending_checkout()

    def retrieval_failure(*_args, **_kwargs):
        raise TimeoutError("provider unavailable")

    monkeypatch.setattr(platform_billing.stripe.checkout.Session, "retrieve", retrieval_failure)

    def unexpected_create(**_kwargs):
        raise AssertionError("a transient lookup failure must not create a new session")

    monkeypatch.setattr(platform_billing.stripe.checkout.Session, "create", unexpected_create)
    monkeypatch.setattr(platform_billing.stripe.Customer, "create", unexpected_create)

    with pytest.raises(RuntimeError, match="Unable to verify"):
        platform_billing.create_checkout("pro")

    row = _studio()
    assert row["platform_checkout_session_id"] == "cs_pending"
    assert row["platform_checkout_plan"] == "pro"


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("session_id", "cs_stale"),
        ("customer_id", "cus_other"),
        ("plan", "starter"),
    ],
)
def test_mismatched_checkout_completion_is_ignored(billing_env, changed, value):
    _seed_pending_checkout()
    event_kwargs = {
        "session_id": "cs_pending",
        "customer_id": "cus_bound",
        "subscription_id": "sub_new",
        "plan": "pro",
    }
    event_kwargs[changed] = value

    platform_billing.handle_webhook_event(_completed_event(**event_kwargs))

    row = _studio()
    assert row["billing_status"] == "trialing"
    assert row["stripe_subscription_id"] == ""
    assert row["platform_checkout_session_id"] == "cs_pending"
    assert row["platform_checkout_plan"] == "pro"
    assert row["platform_checkout_url"] == "https://checkout.test/pending"


def test_valid_bound_completion_activates_and_clears_pending_fields(
    billing_env,
):
    _seed_pending_checkout()

    platform_billing.handle_webhook_event(_completed_event())

    row = _studio()
    assert row["billing_status"] == "active"
    assert row["plan_tier"] == "pro"
    assert row["stripe_subscription_id"] == "sub_new"
    assert row["platform_checkout_session_id"] == ""
    assert row["platform_checkout_plan"] == ""
    assert row["platform_checkout_url"] == ""


def test_canceled_subscription_can_complete_new_exact_bound_checkout(
    billing_env,
):
    _seed_pending_checkout(
        session_id="cs_reactivate",
        customer_id="cus_bound",
        plan="starter",
        billing_status="canceled",
        subscription_id="sub_old",
        url="https://checkout.test/reactivate",
    )

    platform_billing.handle_webhook_event(
        _completed_event(
            session_id="cs_reactivate",
            customer_id="cus_bound",
            subscription_id="sub_reactivated",
            plan="starter",
        )
    )

    row = _studio()
    assert row["billing_status"] == "active"
    assert row["plan_tier"] == "starter"
    assert row["stripe_subscription_id"] == "sub_reactivated"
    assert row["platform_checkout_session_id"] == ""
    assert row["platform_checkout_plan"] == ""
    assert row["platform_checkout_url"] == ""


def test_newer_deletion_is_terminal_against_late_active_event(billing_env):
    db.run(
        """UPDATE studio
           SET stripe_customer_id='cus_bound', stripe_subscription_id='sub_bound',
               billing_status='active', plan_tier='pro'
           WHERE id='default'"""
    )

    platform_billing.handle_webhook_event(
        _subscription_event(event_id="evt_deleted_newer", created=2_000, status="canceled")
    )
    platform_billing.handle_webhook_event(
        _subscription_event(event_id="evt_active_older", created=1_000, status="active")
    )

    row = _studio()
    assert row["billing_status"] == "canceled"
    assert row["plan_tier"] == "solo"
    assert row["stripe_subscription_id"] == "sub_bound"
    assert row["platform_subscription_event_created"] == 2_000
    assert row["platform_subscription_event_id"] == "evt_deleted_newer"


def test_same_subscription_cannot_resurrect_after_cancellation(billing_env):
    db.run(
        """UPDATE studio
           SET stripe_customer_id='cus_bound', stripe_subscription_id='sub_bound',
               billing_status='active', plan_tier='pro'
           WHERE id='default'"""
    )
    platform_billing.handle_webhook_event(
        _subscription_event(event_id="evt_deleted", created=2_000, status="canceled")
    )
    platform_billing.handle_webhook_event(
        _subscription_event(event_id="evt_active_later", created=3_000, status="active")
    )

    row = _studio()
    assert row["billing_status"] == "canceled"
    assert row["platform_subscription_event_created"] == 2_000
    assert row["platform_subscription_event_id"] == "evt_deleted"


@pytest.mark.parametrize(
    "arrival_order",
    [("active", "past_due"), ("past_due", "active")],
)
def test_equal_timestamp_subscription_events_fail_closed(billing_env, arrival_order):
    db.run(
        """UPDATE studio
           SET stripe_customer_id='cus_bound', stripe_subscription_id='sub_bound',
               billing_status='active', plan_tier='pro'
           WHERE id='default'"""
    )
    for index, status in enumerate(arrival_order):
        platform_billing.handle_webhook_event(
            _subscription_event(
                event_id=f"evt_same_second_{index}_{status}",
                created=4_000,
                status=status,
            )
        )

    row = _studio()
    assert row["billing_status"] == "past_due"
    assert row["stripe_subscription_id"] == "sub_bound"
    assert row["platform_subscription_event_created"] == 4_000


@pytest.mark.parametrize(
    "arrival_order",
    [("pro", "starter"), ("starter", "pro")],
)
def test_equal_timestamp_same_status_tier_events_choose_restrictive_tier(
    billing_env, arrival_order
):
    db.run(
        """UPDATE studio
           SET stripe_customer_id='cus_bound', stripe_subscription_id='sub_bound',
               billing_status='active', plan_tier='pro'
           WHERE id='default'"""
    )
    for index, plan_tier in enumerate(arrival_order):
        platform_billing.handle_webhook_event(
            _subscription_event(
                event_id=f"evt_same_second_tier_{index}_{plan_tier}",
                created=5_000,
                status="active",
                plan_tier=plan_tier,
            )
        )

    row = _studio()
    assert row["billing_status"] == "active"
    assert row["plan_tier"] == "starter"
    assert row["stripe_subscription_id"] == "sub_bound"
    assert row["platform_subscription_event_created"] == 5_000


@pytest.mark.parametrize("plan_tier", ["", "enterprise", "solo", "trial"])
def test_active_subscription_with_invalid_tier_falls_back_to_bounded_starter(
    billing_env, plan_tier
):
    db.run(
        """UPDATE studio
           SET stripe_customer_id='cus_bound', stripe_subscription_id='sub_bound',
               billing_status='active', plan_tier='pro'
           WHERE id='default'"""
    )

    platform_billing.handle_webhook_event(
        _subscription_event(
            event_id=f"evt_invalid_tier_{plan_tier or 'missing'}",
            created=6_000,
            status="active",
            plan_tier=plan_tier,
        )
    )

    row = _studio()
    assert row["billing_status"] == "active"
    assert row["plan_tier"] == "starter"
    assert plan_limits.current_tier() == "starter"
    assert plan_limits.LIMITS[row["plan_tier"]]["listings_month"] == 30
    assert plan_limits.LIMITS[row["plan_tier"]]["team_seats"] == 1
    assert plan_limits.LIMITS[row["plan_tier"]]["custom_domain"] is False


@pytest.mark.parametrize("billing_status", ["none", "past_due", "canceled"])
def test_billing_gate_blocks_every_non_active_non_trial_status(
    billing_env, monkeypatch, billing_status
):
    db.run(
        """INSERT INTO studio
           (id, name, slug, active, signup_verified, billing_status)
           VALUES ('billing-gate', 'Billing Gate', 'billing-gate', 1, 1, ?)""",
        (billing_status,),
    )
    tenant.set_studio("billing-gate")
    monkeypatch.setattr(config, "BILLING_ENFORCE", True)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin",
            "raw_path": b"/admin",
            "query_string": b"",
            "headers": [],
            "scheme": "https",
            "server": ("billing-gate.eos.test", 443),
        }
    )

    response = billing_gate.check_access(request)

    assert response is not None
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/billing"


@pytest.mark.parametrize("trial_ends_at", [None, "", "not-a-trial-date"])
def test_billing_gate_redirects_trial_with_missing_or_malformed_end(
    billing_env, monkeypatch, trial_ends_at
):
    db.run(
        """INSERT INTO studio
           (id, name, slug, active, signup_verified, billing_status, trial_ends_at)
           VALUES ('invalid-trial', 'Invalid Trial', 'invalid-trial', 1, 1,
                   'trialing', ?)""",
        (trial_ends_at,),
    )
    tenant.set_studio("invalid-trial")
    monkeypatch.setattr(config, "BILLING_ENFORCE", True)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin",
            "raw_path": b"/admin",
            "query_string": b"",
            "headers": [],
            "scheme": "https",
            "server": ("invalid-trial.eos.test", 443),
        }
    )

    response = billing_gate.check_access(request)

    assert response is not None
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/billing"


@pytest.mark.parametrize(
    ("status", "trial_ends_at", "expected"),
    [
        ("active", None, True),
        ("trialing", "2026-07-27 12:00:01", True),
        ("trialing", "2026-07-27 12:00:00", False),
        ("trialing", None, False),
        ("trialing", "invalid", False),
        ("none", "2026-07-28 12:00:00", False),
        ("incomplete", "2026-07-28 12:00:00", False),
    ],
)
def test_billing_access_predicate_is_an_explicit_fail_closed_allowlist(
    status, trial_ends_at, expected
):
    now = dt.datetime(2026, 7, 27, 12, tzinfo=dt.UTC)

    assert billing_gate.has_billing_access(status, trial_ends_at, now=now) is expected
