"""Billing enforcement gaps: gate transitions, exemptions, and plan-limit lockout.

Complements tests/test_platform_billing_integrity.py (webhook bindings and the
has_billing_access predicate) — these tests cover the check_access side effects,
route exemptions, and plan_limits enforcement on downgrade.
"""

from __future__ import annotations

import importlib

import eos.billing_gate as billing_gate
import eos.config as config
import eos.db as db
import eos.plan_limits as plan_limits
import eos.tenant as tenant
import eos.vocab as vocab
import pytest
from fastapi import HTTPException
from starlette.requests import Request


@pytest.fixture()
def gate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_BASE_URL", "https://eos.test")
    for module in (config, db, tenant, vocab, billing_gate, plan_limits):
        importlib.reload(module)
    config.ensure_dirs()
    db.migrate()
    tenant.set_studio("default")
    yield
    tenant.set_studio("default")


def _seed_studio(
    studio_id: str,
    *,
    active: int = 1,
    signup_verified: int = 1,
    billing_status: str = "trialing",
    trial_ends_at: str | None = "2999-01-01 00:00:00",
    plan_tier: str = "trial",
) -> None:
    db.run(
        """INSERT INTO studio
           (id, name, slug, active, signup_verified, billing_status, trial_ends_at, plan_tier)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            studio_id,
            f"{studio_id} Studio",
            studio_id,
            active,
            signup_verified,
            billing_status,
            trial_ends_at,
            plan_tier,
        ),
    )


def _request(path: str, host: str = "tenant.eos.test") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "scheme": "https",
            "server": (host, 443),
        }
    )


def test_expired_trial_redirects_and_flips_status_to_past_due(gate_env, monkeypatch):
    _seed_studio("expired-trial", trial_ends_at="2020-01-01 00:00:00")
    tenant.set_studio("expired-trial")
    monkeypatch.setattr(config, "BILLING_ENFORCE", True)

    response = billing_gate.check_access(_request("/admin"))

    assert response is not None
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/billing"
    row = db.one("SELECT billing_status FROM studio WHERE id='expired-trial'")
    assert row["billing_status"] == "past_due"


def test_unexpired_trial_and_active_subscription_keep_access(gate_env, monkeypatch):
    _seed_studio("live-trial", trial_ends_at="2999-01-01 00:00:00")
    _seed_studio("paid-up", billing_status="active", trial_ends_at=None)
    monkeypatch.setattr(config, "BILLING_ENFORCE", True)

    tenant.set_studio("live-trial")
    assert billing_gate.check_access(_request("/admin")) is None
    tenant.set_studio("paid-up")
    assert billing_gate.check_access(_request("/admin")) is None

    assert (
        db.one("SELECT billing_status FROM studio WHERE id='live-trial'")["billing_status"]
        == "trialing"
    )


def test_past_due_lockout_does_not_mutate_status(gate_env, monkeypatch):
    _seed_studio("past-due", billing_status="past_due", trial_ends_at="2020-01-01 00:00:00")
    tenant.set_studio("past-due")
    monkeypatch.setattr(config, "BILLING_ENFORCE", True)

    response = billing_gate.check_access(_request("/admin"))

    assert response is not None
    assert response.headers["location"] == "/admin/billing"
    assert (
        db.one("SELECT billing_status FROM studio WHERE id='past-due'")["billing_status"]
        == "past_due"
    )


def test_billing_enforce_disabled_never_gates(gate_env, monkeypatch):
    _seed_studio("lenient", billing_status="past_due", trial_ends_at="2020-01-01 00:00:00")
    tenant.set_studio("lenient")
    monkeypatch.setattr(config, "BILLING_ENFORCE", False)

    assert billing_gate.check_access(_request("/admin")) is None
    assert (
        db.one("SELECT billing_status FROM studio WHERE id='lenient'")["billing_status"]
        == "past_due"
    )


@pytest.mark.parametrize(
    "path",
    ["/admin/billing", "/admin/logout", "/stripe/platform/webhook", "/admin/login"],
)
def test_billing_routes_stay_reachable_during_lockout(gate_env, monkeypatch, path):
    _seed_studio("locked", billing_status="past_due", trial_ends_at=None)
    tenant.set_studio("locked")
    monkeypatch.setattr(config, "BILLING_ENFORCE", True)

    assert billing_gate.check_access(_request(path)) is None


def test_public_routes_are_never_billing_gated(gate_env, monkeypatch):
    _seed_studio("public-tenant", billing_status="past_due", trial_ends_at=None)
    tenant.set_studio("public-tenant")
    monkeypatch.setattr(config, "BILLING_ENFORCE", True)

    assert billing_gate.check_access(_request("/g/some-gallery")) is None
    assert billing_gate.check_access(_request("/book")) is None


def test_solo_default_studio_is_never_billing_gated(gate_env, monkeypatch):
    db.run("UPDATE studio SET billing_status='past_due' WHERE id='default'")
    tenant.set_studio("default")
    monkeypatch.setattr(config, "BILLING_ENFORCE", True)

    assert billing_gate.check_access(_request("/admin")) is None


def test_inactive_studio_admin_redirects_to_login_public_404s(gate_env, monkeypatch):
    _seed_studio("suspended", active=0, billing_status="active")
    tenant.set_studio("suspended")
    monkeypatch.setattr(config, "BILLING_ENFORCE", True)

    admin_response = billing_gate.check_access(_request("/admin"))
    assert admin_response is not None
    assert admin_response.status_code == 303
    assert admin_response.headers["location"] == "/admin/login?suspended=1"

    public_response = billing_gate.check_access(_request("/g/some-gallery"))
    assert public_response is not None
    assert public_response.status_code == 404


def test_unverified_signup_redirects_before_billing_check(gate_env, monkeypatch):
    _seed_studio("unverified", signup_verified=0, billing_status="past_due", trial_ends_at=None)
    tenant.set_studio("unverified")
    monkeypatch.setattr(config, "SIGNUP_ENABLED", True)
    monkeypatch.setattr(config, "BILLING_ENFORCE", True)

    response = billing_gate.check_access(_request("/admin"))

    assert response is not None
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/verify-pending"


def test_plan_limits_enforce_trial_and_starter_caps(gate_env):
    _seed_studio("capped", plan_tier="trial")
    tenant.set_studio("capped")

    assert plan_limits.current_tier() == "trial"
    plan_limits.check_listing_create(current_month_count=14)
    with pytest.raises(HTTPException) as exc_info:
        plan_limits.check_listing_create(current_month_count=15)
    assert exc_info.value.status_code == 403
    assert "Upgrade to Pro" in exc_info.value.detail

    db.run("UPDATE studio SET plan_tier='starter' WHERE id='capped'")
    plan_limits.check_listing_create(current_month_count=29)
    with pytest.raises(HTTPException):
        plan_limits.check_listing_create(current_month_count=30)

    db.run("UPDATE studio SET plan_tier='pro' WHERE id='capped'")
    plan_limits.check_listing_create(current_month_count=100000)


def test_downgrade_enforces_team_seat_and_token_and_webhook_caps(gate_env):
    _seed_studio("downgraded", plan_tier="starter")
    tenant.set_studio("downgraded")

    with pytest.raises(HTTPException) as exc_info:
        plan_limits.check_team_seat(current_count=1)
    assert exc_info.value.status_code == 403
    assert "Starter" in exc_info.value.detail

    with pytest.raises(HTTPException):
        plan_limits.check_api_token(current_count=2)
    with pytest.raises(HTTPException):
        plan_limits.check_webhook(current_count=2)

    db.run("UPDATE studio SET plan_tier='pro' WHERE id='downgraded'")
    plan_limits.check_team_seat(current_count=4)
    with pytest.raises(HTTPException):
        plan_limits.check_team_seat(current_count=5)
    plan_limits.check_api_token(current_count=9)
    plan_limits.check_webhook(current_count=9)


def test_downgrade_locks_custom_domain_and_storage_caps(gate_env):
    _seed_studio("storage-capped", plan_tier="trial")
    tenant.set_studio("storage-capped")

    with pytest.raises(HTTPException) as exc_info:
        plan_limits.check_custom_domain()
    assert exc_info.value.status_code == 403
    assert "Pro" in exc_info.value.detail

    five_gb = 5 * 1024 * 1024 * 1024
    plan_limits.check_storage(current_bytes=five_gb - 1)
    with pytest.raises(HTTPException) as exc_info:
        plan_limits.check_storage(current_bytes=five_gb)
    assert exc_info.value.status_code == 403
    assert "5 GB" in exc_info.value.detail

    db.run("UPDATE studio SET plan_tier='pro' WHERE id='storage-capped'")
    plan_limits.check_custom_domain()
    plan_limits.check_storage(current_bytes=100 * 1024 * 1024 * 1024 - 1)
    with pytest.raises(HTTPException):
        plan_limits.check_storage(current_bytes=100 * 1024 * 1024 * 1024)


def test_unknown_plan_tier_falls_back_to_unlimited_solo(gate_env, monkeypatch):
    _seed_studio("mystery", plan_tier="pro")
    tenant.set_studio("mystery")

    assert plan_limits.limits_for("enterprise")["label"] == "Solo"

    with monkeypatch.context() as mp:
        mp.setattr(
            plan_limits.db,
            "one",
            lambda *_args, **_kwargs: {"plan_tier": "enterprise"},
        )
        assert plan_limits.current_tier() == "solo"
        assert plan_limits.limits_for()["label"] == "Solo"
        plan_limits.check_listing_create(current_month_count=100000)
        plan_limits.check_custom_domain()
