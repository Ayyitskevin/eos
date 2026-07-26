"""Route-driven acceptance tests for the production beta activation journey."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from types import SimpleNamespace

import eos.api_tokens as api_tokens
import eos.automations as automations
import eos.commerce as commerce
import eos.config as config
import eos.db as db
import eos.invites as invites
import eos.mailer as mailer
import eos.main as main
import eos.onboarding as onboarding
import eos.onboarding_wizard as onboarding_wizard
import eos.platform_billing as platform_billing
import eos.referrals as referrals
import eos.routes.auth as auth_routes
import eos.routes.booking as booking_routes
import eos.routes.onboarding_routes as onboarding_routes
import eos.routes.signup as signup_routes
import eos.routes.site as site_routes
import eos.routes.studio_admin as studio_admin_routes
import eos.security as security
import eos.signup_verify as signup_verify
import eos.stripe_checkout as stripe_checkout
import eos.stripe_connect as stripe_connect
import eos.studio as studio
import eos.studio_seed as studio_seed
import eos.tenant as tenant
import eos.users as users
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

APEX_HOST = "localhost:8410"
BASE_URL = f"http://{APEX_HOST}"
PASSWORD = "beta-pass-2026"


@pytest.fixture()
def beta_env(tmp_path, monkeypatch):
    """Fresh hosted-SaaS app with every external integration disabled."""
    values = {
        "EOS_DATA_DIR": str(tmp_path / "data"),
        "EOS_SECRET_KEY": "test-secret-key-32chars-minimum!!",
        "EOS_ADMIN_PASSWORD": "test-admin-pass",
        "EOS_SAAS_MODE": "true",
        "EOS_SIGNUP_ENABLED": "true",
        "EOS_SIGNUP_AUTO_VERIFY_LOCAL": "true",
        "EOS_SIGNUP_INVITE_ONLY": "false",
        "EOS_BASE_DOMAIN": APEX_HOST,
        "EOS_BASE_URL": BASE_URL,
        "EOS_COOKIE_SECURE": "false",
        "EOS_BILLING_ENFORCE": "false",
        "EOS_DEMO_ENABLED": "false",
        "EOS_EMAIL_PROVIDER": "smtp",
        "EOS_GMAIL_USER": "",
        "EOS_GMAIL_APP_PASSWORD": "",
        "EOS_POSTMARK_API_KEY": "",
        "EOS_POSTMARK_FROM_EMAIL": "",
        "EOS_STRIPE_SECRET_KEY": "",
        "EOS_STRIPE_PLATFORM_SECRET_KEY": "",
        "EOS_STRIPE_PRICE_STARTER": "",
        "EOS_STRIPE_PRICE_PRO": "",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    modules = (
        config,
        db,
        security,
        tenant,
        api_tokens,
        invites,
        mailer,
        signup_verify,
        users,
        studio_seed,
        platform_billing,
        stripe_connect,
        stripe_checkout,
        studio,
        referrals,
        automations,
        commerce,
        onboarding,
        onboarding_wizard,
        auth_routes,
        booking_routes,
        onboarding_routes,
        signup_routes,
        site_routes,
        studio_admin_routes,
        main,
    )
    for module in modules:
        importlib.reload(module)

    config.ensure_dirs()
    db.migrate()
    tenant.set_studio("default")
    return SimpleNamespace(
        app=main.app,
        db=db,
        tenant=tenant,
        commerce=commerce,
        mailer=mailer,
        security=security,
        stripe_checkout=stripe_checkout,
    )


def _host(slug: str) -> str:
    return f"{slug}.{APEX_HOST}"


async def _signup(client: AsyncClient, slug: str):
    response = await client.post(
        "/signup",
        headers={"host": APEX_HOST},
        data={
            "studio_name": f"{slug.title()} Studio",
            "slug": slug,
            "owner_name": "Beta Owner",
            "owner_email": f"owner@{slug}.test",
            "owner_password": PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response


async def _login(client: AsyncClient, slug: str) -> dict[str, str]:
    response = await client.post(
        "/admin/login",
        headers={"host": _host(slug)},
        data={"email": f"owner@{slug}.test", "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    session = response.cookies.get(security.ADMIN_COOKIE)
    assert session
    return {"host": _host(slug), "cookie": f"{security.ADMIN_COOKIE}={session}"}


async def _quick_launch(client: AsyncClient, slug: str) -> dict[str, str]:
    headers = await _login(client, slug)
    response = await client.post(
        "/admin/onboarding/quick-launch",
        headers=headers,
        data={
            "headline": "Bright, MLS-ready photos in 24 hours",
            "service_area": "Austin and surrounding counties",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return headers


async def _booking_inputs(client: AsyncClient, slug: str) -> tuple[int, str]:
    response = await client.get("/book", headers={"host": _host(slug)})
    assert response.status_code == 200, response.text
    package = re.search(r'name="package_id" value="(\d+)"', response.text)
    slot = re.search(r'<option value="([^"]+)"', response.text)
    assert package and slot
    return int(package.group(1)), slot.group(1)


def _booking_payload(
    *,
    package_id: int,
    scheduled_at: str,
    request_key: str,
    email: str = "agent@example.com",
    address: str = "101 Beta Journey Ave, Austin TX",
    promo_code: str = "",
) -> dict:
    return {
        "name": "Agent One",
        "email": email,
        "phone": "512-555-0100",
        "property_address": address,
        "package_id": str(package_id),
        "scheduled_at": scheduled_at,
        "signer_name": "Agent One",
        "promo_code": promo_code,
        "request_key": request_key,
    }


def _count(table: str, studio_id: str) -> int:
    row = db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE studio_id=?", (studio_id,))
    return int(row["n"])


@pytest.mark.asyncio
async def test_documented_localhost_port_signup_builds_one_port_origin(beta_env):
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        response = await _signup(client, "port-safe")

    expected = "http://port-safe.localhost:8410/admin/login"
    assert response.headers["location"] == expected
    assert response.headers["location"].count(":8410") == 1
    assert tenant.subdomain_from_host("port-safe.localhost:8410") == "port-safe"
    assert tenant.studio_origin(slug="port-safe") == "http://port-safe.localhost:8410"


@pytest.mark.asyncio
async def test_unknown_and_malformed_hosts_fail_closed_even_with_tenant_session(beta_env):
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await _signup(client, "host-safe")
        auth_headers = await _quick_launch(client, "host-safe")
        package_id, scheduled_at = await _booking_inputs(client, "host-safe")
        payload = _booking_payload(
            package_id=package_id,
            scheduled_at=scheduled_at,
            request_key="host-must-not-leak",
        )

        unknown_headers = {
            "host": _host("unknown"),
            "cookie": auth_headers["cookie"],
        }
        malformed_headers = {
            "host": "host-safe.localhost:notaport",
            "cookie": auth_headers["cookie"],
        }
        unknown_get = await client.get("/book", headers=unknown_headers)
        malformed_get = await client.get("/book", headers=malformed_headers)
        unknown_post = await client.post("/book", headers=unknown_headers, data=payload)
        malformed_post = await client.post("/book", headers=malformed_headers, data=payload)

    assert unknown_get.status_code == 404
    assert unknown_post.status_code == 404
    assert malformed_get.status_code == 400
    assert malformed_post.status_code == 400
    assert "Host-Safe Studio" not in unknown_get.text
    assert _count("inquiries", "host-safe") == 0
    assert _count("inquiries", "default") == 0


@pytest.mark.asyncio
async def test_unverified_and_unpublished_studio_cannot_get_or_post_booking(beta_env, monkeypatch):
    monkeypatch.setattr(beta_env.mailer, "configured", lambda: True)
    monkeypatch.setattr(beta_env.mailer, "send_platform", lambda *_args, **_kwargs: None)

    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await _signup(client, "not-live")
        row = db.one(
            """SELECT signup_verified, signup_verify_token
               FROM studio WHERE id='not-live'"""
        )
        assert row["signup_verified"] == 0
        token = row["signup_verify_token"]

        blocked_get = await client.get("/book", headers={"host": _host("not-live")})
        blocked_post = await client.post(
            "/book",
            headers={"host": _host("not-live")},
            data={
                "name": "Blocked Agent",
                "email": "blocked@example.com",
                "property_address": "1 Closed Lane",
                "package_id": "1",
                "scheduled_at": "2099-01-01 10:00:00",
                "signer_name": "Blocked Agent",
                "request_key": "not-verified",
            },
        )
        verified = await client.get(
            f"/verify/{token}",
            headers={"host": _host("not-live")},
            follow_redirects=False,
        )
        assert verified.status_code == 303

        db.run(
            """UPDATE studio_profiles SET booking_enabled=1, published=0
               WHERE studio_id='not-live'"""
        )
        unpublished_get = await client.get("/book", headers={"host": _host("not-live")})
        unpublished_post = await client.post(
            "/book",
            headers={"host": _host("not-live")},
            data={
                "name": "Blocked Agent",
                "email": "blocked@example.com",
                "property_address": "1 Closed Lane",
                "package_id": "1",
                "scheduled_at": "2099-01-01 10:00:00",
                "signer_name": "Blocked Agent",
                "request_key": "not-published",
            },
        )

    assert blocked_get.status_code == 404
    assert blocked_post.status_code == 404
    assert unpublished_get.status_code == 404
    assert unpublished_post.status_code == 404
    assert _count("inquiries", "not-live") == 0


@pytest.mark.asyncio
async def test_readiness_is_live_and_revokes_booking_when_configuration_regresses(beta_env):
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await _signup(client, "ready-live")
        admin_headers = await _login(client, "ready-live")

        before = await client.get("/admin/onboarding", headers=admin_headers)
        before_public = await client.get("/book", headers={"host": _host("ready-live")})
        assert before.status_code == 200
        assert "Booking link (share with agents)" not in before.text
        assert before_public.status_code == 404

        launched = await client.post(
            "/admin/onboarding/quick-launch",
            headers=admin_headers,
            data={
                "headline": "Ready when your listing is",
                "service_area": "Central Texas",
            },
            follow_redirects=False,
        )
        assert launched.status_code == 303
        ready_page = await client.get("/admin/onboarding", headers=admin_headers)
        ready_public = await client.get("/book", headers={"host": _host("ready-live")})
        assert ready_page.status_code == 200
        assert "http://ready-live.localhost:8410/book" in ready_page.text
        assert ready_public.status_code == 200

        disabled = await client.post(
            "/admin/studio",
            headers=admin_headers,
            data={
                "name": "Ready-Live Studio",
                "headline": "Ready when your listing is",
                "service_area": "Central Texas",
                "published": "true",
            },
            follow_redirects=False,
        )
        assert disabled.status_code == 303
        regressed_page = await client.get("/admin/onboarding", headers=admin_headers)
        regressed_public = await client.get("/book", headers={"host": _host("ready-live")})

    assert "Booking link (share with agents)" not in regressed_page.text
    assert regressed_public.status_code == 404
    profile = db.one(
        """SELECT onboarding_done, published, booking_enabled
           FROM studio_profiles WHERE studio_id='ready-live'"""
    )
    assert profile["onboarding_done"] == 1
    assert profile["published"] == 1
    assert profile["booking_enabled"] == 0
    tenant.set_studio("ready-live")
    assert onboarding_wizard.status()["done"] is False


@pytest.mark.asyncio
async def test_booking_request_key_replay_creates_one_complete_pending_workflow(beta_env):
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await _signup(client, "replay-safe")
        await _quick_launch(client, "replay-safe")
        package_id, scheduled_at = await _booking_inputs(client, "replay-safe")

        tenant.set_studio("replay-safe")
        referrer_id = db.run(
            """INSERT INTO clients (studio_id, name, email)
               VALUES ('replay-safe', 'Referring Agent', 'referrer@example.com')"""
        )
        db.run(
            """INSERT INTO referral_codes
               (studio_id, code, credit_cents, referrer_client_id, max_uses)
               VALUES ('replay-safe', 'REFER25', 2500, ?, 10)""",
            (referrer_id,),
        )
        payload = _booking_payload(
            package_id=package_id,
            scheduled_at=scheduled_at,
            request_key="booking-replay-001",
            promo_code="REFER25",
        )
        first = await client.post(
            "/book",
            headers={"host": _host("replay-safe")},
            data=payload,
            follow_redirects=False,
        )
        second = await client.post(
            "/book",
            headers={"host": _host("replay-safe")},
            data=payload,
            follow_redirects=False,
        )

    assert first.status_code == 303
    assert second.status_code == 303
    assert second.headers["location"] == first.headers["location"]
    for table in ("inquiries", "listings", "appointments", "invoices"):
        assert _count(table, "replay-safe") == 1
    assert _count("referral_redemptions", "replay-safe") == 1
    referral = db.one(
        """SELECT uses FROM referral_codes
           WHERE studio_id='replay-safe' AND code='REFER25'"""
    )
    assert referral["uses"] == 0
    redemption = db.one("SELECT status FROM referral_redemptions WHERE studio_id='replay-safe'")
    assert redemption["status"] == "reserved"


@pytest.mark.asyncio
async def test_invoice_failure_rolls_back_workflow_credit_and_referral(beta_env, monkeypatch):
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await _signup(client, "rollback-safe")
        await _quick_launch(client, "rollback-safe")
        package_id, scheduled_at = await _booking_inputs(client, "rollback-safe")

        tenant.set_studio("rollback-safe")
        client_id = db.run(
            """INSERT INTO clients (studio_id, name, email, credit_cents)
               VALUES ('rollback-safe', 'Existing Agent', 'existing@example.com', 1200)"""
        )
        referral_id = db.run(
            """INSERT INTO referral_codes
               (studio_id, code, credit_cents, max_uses)
               VALUES ('rollback-safe', 'ROLLBACK25', 2500, 10)"""
        )

        def fail_invoice(*_args, **_kwargs):
            raise RuntimeError("injected invoice failure")

        monkeypatch.setattr(
            beta_env.commerce.invoices,
            "create_deposit_invoice",
            fail_invoice,
        )
        failing_transport = ASGITransport(app=beta_env.app, raise_app_exceptions=False)
        async with AsyncClient(transport=failing_transport, base_url=BASE_URL) as failing_client:
            response = await failing_client.post(
                "/book",
                headers={"host": _host("rollback-safe")},
                data=_booking_payload(
                    package_id=package_id,
                    scheduled_at=scheduled_at,
                    request_key="rollback-booking-001",
                    email="existing@example.com",
                    promo_code="ROLLBACK25",
                ),
                follow_redirects=False,
            )

    assert response.status_code == 500
    for table in (
        "inquiries",
        "listings",
        "appointments",
        "proposals",
        "invoices",
        "referral_redemptions",
    ):
        assert _count(table, "rollback-safe") == 0
    restored_client = db.one(
        "SELECT credit_cents FROM clients WHERE id=? AND studio_id='rollback-safe'",
        (client_id,),
    )
    assert restored_client["credit_cents"] == 1200
    assert _count("credit_ledger", "rollback-safe") == 0
    referral = db.one(
        "SELECT uses FROM referral_codes WHERE id=? AND studio_id='rollback-safe'",
        (referral_id,),
    )
    assert referral["uses"] == 0


@pytest.mark.asyncio
async def test_offline_deposit_stays_pending_and_is_visible_without_stripe(beta_env):
    assert beta_env.stripe_checkout.payment_rail() == ("unavailable", None)
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await _signup(client, "offline-safe")
        await _quick_launch(client, "offline-safe")
        package_id, scheduled_at = await _booking_inputs(client, "offline-safe")
        response = await client.post(
            "/book",
            headers={"host": _host("offline-safe")},
            data=_booking_payload(
                package_id=package_id,
                scheduled_at=scheduled_at,
                request_key="offline-booking-001",
            ),
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/booking/")
        confirmation = await client.get(
            response.headers["location"],
            headers={"host": _host("offline-safe")},
        )

    assert confirmation.status_code == 200
    assert "Deposit required to confirm your slot." in confirmation.text
    assert "You're booked!" not in confirmation.text
    assert "Pay deposit" not in confirmation.text
    inquiry = db.one(
        """SELECT status, appointment_id, listing_id, invoice_id, deposit_cents
           FROM inquiries
           WHERE studio_id='offline-safe' AND request_key='offline-booking-001'"""
    )
    assert inquiry["status"] == "pending_payment"
    assert inquiry["deposit_cents"] > 0
    appointment = db.one(
        "SELECT status FROM appointments WHERE id=? AND studio_id='offline-safe'",
        (inquiry["appointment_id"],),
    )
    listing = db.one(
        "SELECT status FROM listings WHERE id=? AND studio_id='offline-safe'",
        (inquiry["listing_id"],),
    )
    invoice = db.one(
        "SELECT status FROM invoices WHERE id=? AND studio_id='offline-safe'",
        (inquiry["invoice_id"],),
    )
    assert appointment["status"] == "proposed"
    assert listing["status"] == "lead"
    assert invoice["status"] == "sent"


@pytest.mark.asyncio
async def test_same_slot_different_request_keys_accepts_once(beta_env):
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await _signup(client, "slot-safe")
        await _quick_launch(client, "slot-safe")
        package_id, scheduled_at = await _booking_inputs(client, "slot-safe")

    async def submit(suffix: str):
        own_transport = ASGITransport(app=beta_env.app)
        async with AsyncClient(transport=own_transport, base_url=BASE_URL) as own_client:
            return await own_client.post(
                "/book",
                headers={"host": _host("slot-safe")},
                data=_booking_payload(
                    package_id=package_id,
                    scheduled_at=scheduled_at,
                    request_key=f"same-slot-{suffix}",
                    email=f"agent-{suffix}@example.com",
                    address=f"{suffix} Same Slot Way, Austin TX",
                ),
                follow_redirects=False,
            )

    first, second = await asyncio.gather(submit("one"), submit("two"))
    assert sorted((first.status_code, second.status_code)) == [303, 400]
    rejected = first if first.status_code == 400 else second
    assert "slot no longer available" in rejected.text
    for table in ("inquiries", "listings", "appointments", "invoices"):
        assert _count(table, "slot-safe") == 1


@pytest.mark.asyncio
async def test_failed_verification_delivery_is_visible_and_resend_recovers(beta_env, monkeypatch):
    sent = []

    def fail_delivery(*_args, **_kwargs):
        raise RuntimeError("platform mail unavailable")

    monkeypatch.setattr(beta_env.mailer, "configured", lambda: True)
    monkeypatch.setattr(beta_env.mailer, "send_platform", fail_delivery)
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        response = await _signup(client, "verify-retry")
        assert response.headers["location"] == ("http://verify-retry.localhost:8410/admin/login")
        assert "cap=" not in response.headers["location"]
        row = db.one(
            """SELECT signup_verified, provisioning_status, provisioning_error
               FROM studio WHERE id='verify-retry'"""
        )
        assert row["provisioning_status"] == "degraded"
        assert "verification:" in row["provisioning_error"]

        monkeypatch.setattr(
            beta_env.mailer,
            "send_platform",
            lambda *args, **_kwargs: sent.append(args),
        )
        unauthorized = await client.get(
            "/admin/verify-pending",
            headers={"host": _host("verify-retry")},
            follow_redirects=False,
        )
        assert unauthorized.status_code == 303
        assert unauthorized.headers["location"] == "/admin/login"
        admin_headers = await _login(client, "verify-retry")
        pending = await client.get("/admin/verify-pending", headers=admin_headers)
        assert pending.status_code == 200
        assert pending.headers["cache-control"] == "private, no-store"
        assert "cap=" not in pending.text
        assert 'name="cap"' not in pending.text
        assert "owner@verify-retry.test" not in pending.text
        assert "o***@verify-retry.test" in pending.text
        csrf = pending.cookies.get(security.CSRF_COOKIE)
        assert csrf
        resent = await client.post(
            "/admin/verify-pending/resend",
            headers={
                **admin_headers,
                "cookie": (f"{admin_headers['cookie']}; {security.CSRF_COOKIE}={csrf}"),
                "sec-fetch-site": "same-origin",
            },
            data={security.CSRF_FORM: csrf},
            follow_redirects=False,
        )
        assert resent.status_code == 303
        assert resent.headers["location"] == "/admin/verify-pending?resent=1"

    recovered = db.one(
        "SELECT provisioning_status, provisioning_error FROM studio WHERE id='verify-retry'"
    )
    assert dict(recovered) == {"provisioning_status": "ready", "provisioning_error": None}
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_booking_intent_failure_rolls_back_zero_deposit_workflow(beta_env, monkeypatch):
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await _signup(client, "intent-rollback")
        await _quick_launch(client, "intent-rollback")
        package_id, scheduled_at = await _booking_inputs(client, "intent-rollback")
        db.run(
            "UPDATE service_packages SET deposit_cents=0 WHERE id=? AND studio_id='intent-rollback'",
            (package_id,),
        )

        def fail_intent(*_args, **_kwargs):
            raise RuntimeError("injected intent persistence failure")

        monkeypatch.setattr(automations, "_trigger", fail_intent)
        failing_transport = ASGITransport(app=beta_env.app, raise_app_exceptions=False)
        async with AsyncClient(transport=failing_transport, base_url=BASE_URL) as failing_client:
            response = await failing_client.post(
                "/book",
                headers={"host": _host("intent-rollback")},
                data=_booking_payload(
                    package_id=package_id,
                    scheduled_at=scheduled_at,
                    request_key="intent-rollback-001",
                ),
                follow_redirects=False,
            )

    assert response.status_code == 500
    for table in ("clients", "inquiries", "listings", "appointments", "proposals", "invoices"):
        assert _count(table, "intent-rollback") == 0


@pytest.mark.asyncio
async def test_unconfigured_loopback_aliases_do_not_reach_platform_host(beta_env):
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        configured = await client.get("/signup", headers={"host": APEX_HOST})
        testserver = await client.get("/signup", headers={"host": "testserver"})
        loopback = await client.get("/signup", headers={"host": "127.0.0.1"})

    assert configured.status_code == 200
    assert testserver.status_code == 404
    assert loopback.status_code == 404


@pytest.mark.asyncio
async def test_api_booking_request_key_replay_returns_original_result(beta_env):
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await _signup(client, "api-replay")
        await _quick_launch(client, "api-replay")
        package_id, scheduled_at = await _booking_inputs(client, "api-replay")
        tenant.set_studio("api-replay")
        db.run(
            "UPDATE service_packages SET deposit_cents=0 WHERE id=? AND studio_id='api-replay'",
            (package_id,),
        )
        _token_id, raw_token = api_tokens.create_token(label="beta replay")
        payload = {
            "name": "API Agent",
            "email": "api-agent@example.com",
            "phone": "512-555-0111",
            "property_address": "88 Replay API Way, Austin TX",
            "request_key": "api-replay-001",
            "package_id": package_id,
            "scheduled_at": scheduled_at,
            "signer_name": "API Agent",
        }
        headers = {
            "host": _host("api-replay"),
            "authorization": f"Bearer {raw_token}",
        }
        first = await client.post("/api/v1/bookings", headers=headers, json=payload)
        second = await client.post("/api/v1/bookings", headers=headers, json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    for table in ("clients", "inquiries", "listings", "appointments", "proposals"):
        assert _count(table, "api-replay") == 1


def test_operator_can_retry_crash_interrupted_provisioning(beta_env, monkeypatch):
    del beta_env
    db.run(
        """INSERT INTO studio
           (id, name, slug, contact_email, provisioning_status, provisioning_error)
           VALUES ('recover-setup', 'Recover Setup', 'recover-setup',
                   'owner@recover.test', 'provisioning', NULL)"""
    )
    tenant.set_studio("recover-setup")
    calls = []
    monkeypatch.setattr(onboarding.config, "SIGNUP_ENABLED", False)
    monkeypatch.setattr(platform_billing, "is_configured", lambda: True)
    monkeypatch.setattr(
        platform_billing,
        "ensure_customer",
        lambda **kwargs: calls.append(kwargs) or "cus_recovered",
    )

    recovered = onboarding.retry_provisioning()

    assert recovered == {"provisioning_status": "ready", "provisioning_error": None}
    assert calls == [{"email": "owner@recover.test", "name": "Recover Setup"}]
    row = db.one(
        "SELECT provisioning_status, provisioning_error FROM studio WHERE id=?",
        ("recover-setup",),
    )
    assert dict(row) == recovered
    tenant.set_studio("default")


@pytest.mark.asyncio
async def test_booking_replay_key_rejects_changed_payload_and_short_keys(beta_env):
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await _signup(client, "replay-bound")
        await _quick_launch(client, "replay-bound")
        package_id, scheduled_at = await _booking_inputs(client, "replay-bound")
        payload = _booking_payload(
            package_id=package_id,
            scheduled_at=scheduled_at,
            request_key="booking-bound-001",
        )
        first = await client.post(
            "/book",
            headers={"host": _host("replay-bound")},
            data=payload,
            follow_redirects=False,
        )
        changed = await client.post(
            "/book",
            headers={"host": _host("replay-bound")},
            data={**payload, "property_address": "202 Different Payload Way"},
            follow_redirects=False,
        )
        short = await client.post(
            "/book",
            headers={"host": _host("replay-bound")},
            data={**payload, "request_key": "short"},
            follow_redirects=False,
        )

    assert first.status_code == 303
    assert changed.status_code == 409
    assert "already used for different details" in changed.text
    assert short.status_code == 400
    assert _count("inquiries", "replay-bound") == 1
    inquiry = db.one("SELECT property_address FROM inquiries WHERE studio_id='replay-bound'")
    assert inquiry["property_address"] == "101 Beta Journey Ave, Austin TX"


@pytest.mark.asyncio
async def test_duplicate_addon_ids_are_priced_once_across_booking_artifacts(beta_env):
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await _signup(client, "addon-canonical")
        await _quick_launch(client, "addon-canonical")
        package_id, scheduled_at = await _booking_inputs(client, "addon-canonical")

    tenant.set_studio("addon-canonical")
    package = db.one(
        """SELECT price_cents FROM service_packages
           WHERE id=? AND studio_id=?""",
        (package_id, "addon-canonical"),
    )
    addon = db.one(
        """SELECT id, name, price_cents FROM service_addons
           WHERE studio_id=? AND slug=?""",
        ("addon-canonical", "drone"),
    )

    result = commerce.create_booking(
        name="Duplicate Add-on Agent",
        email="duplicate-addon@example.com",
        phone="512-555-0120",
        property_address="120 Canonical Add-on Way, Austin TX",
        package_id=package_id,
        scheduled_at=scheduled_at,
        addon_ids=[addon["id"], addon["id"]],
        signer_name="Duplicate Add-on Agent",
        request_key="duplicate-addon-001",
    )

    inquiry = db.one(
        """SELECT addon_ids, total_cents, invoice_id FROM inquiries
           WHERE id=? AND studio_id=?""",
        (result["inquiry_id"], "addon-canonical"),
    )
    invoice = db.one(
        "SELECT line_items FROM invoices WHERE id=? AND studio_id=?",
        (inquiry["invoice_id"], "addon-canonical"),
    )
    proposal = db.one(
        """SELECT line_items, total_cents FROM proposals
           WHERE listing_id=? AND studio_id=?""",
        (result["listing_id"], "addon-canonical"),
    )
    expected_total = package["price_cents"] + addon["price_cents"]
    invoice_items = json.loads(invoice["line_items"])
    proposal_items = json.loads(proposal["line_items"])

    assert json.loads(inquiry["addon_ids"]) == [addon["id"]]
    assert inquiry["total_cents"] == expected_total
    assert sum(item["qty"] * item["unit_cents"] for item in invoice_items) == expected_total
    assert sum(item["qty"] * item["unit_cents"] for item in proposal_items) == expected_total
    assert [item["label"] for item in invoice_items].count(addon["name"]) == 1
    assert [item["label"] for item in proposal_items].count(addon["name"]) == 1


@pytest.mark.asyncio
async def test_invalid_addon_ids_leave_no_booking_workflow_side_effects(beta_env):
    transport = ASGITransport(app=beta_env.app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await _signup(client, "addon-atomic")
        await _quick_launch(client, "addon-atomic")
        package_id, scheduled_at = await _booking_inputs(client, "addon-atomic")
        await _signup(client, "addon-foreign")

    tenant.set_studio("addon-atomic")
    inactive = db.one(
        """SELECT id FROM service_addons
           WHERE studio_id=? AND slug=?""",
        ("addon-atomic", "drone"),
    )
    foreign = db.one(
        """SELECT id FROM service_addons
           WHERE studio_id=? AND slug=?""",
        ("addon-foreign", "drone"),
    )
    db.run(
        "UPDATE service_addons SET active=0 WHERE id=? AND studio_id=?",
        (inactive["id"], "addon-atomic"),
    )
    nonexistent_id = db.one("SELECT COALESCE(MAX(id), 0) + 1000 AS id FROM service_addons")["id"]
    workflow_tables = (
        "clients",
        "inquiries",
        "listings",
        "appointments",
        "proposals",
        "invoices",
    )
    baseline = {table: _count(table, "addon-atomic") for table in workflow_tables}

    for label, addon_id in (
        ("inactive", inactive["id"]),
        ("foreign", foreign["id"]),
        ("nonexistent", nonexistent_id),
    ):
        with pytest.raises(HTTPException) as exc_info:
            commerce.create_booking(
                name=f"{label.title()} Add-on Agent",
                email=f"{label}-addon@example.com",
                phone="512-555-0121",
                property_address=f"121 {label.title()} Add-on Way, Austin TX",
                package_id=package_id,
                scheduled_at=scheduled_at,
                addon_ids=[addon_id],
                signer_name=f"{label.title()} Add-on Agent",
                request_key=f"{label}-addon-001",
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "invalid add-on selection"
        assert {table: _count(table, "addon-atomic") for table in workflow_tables} == baseline
