"""Route-driven production-beta smoke journey."""

from __future__ import annotations

import asyncio
import importlib
import io
import re
import socket
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import eos.appointments as appointments
import eos.automations as automations
import eos.commerce as commerce
import eos.config as config
import eos.db as db
import eos.delivery_notify as delivery_notify
import eos.galleries as galleries
import eos.invites as invites
import eos.jobs as jobs
import eos.listings as listings
import eos.mailer as mailer
import eos.main as main
import eos.onboarding as onboarding
import eos.onboarding_wizard as onboarding_wizard
import eos.platform_billing as platform_billing
import eos.portal as portal
import eos.referrals as referrals
import eos.routes.appointments as appointment_routes
import eos.routes.auth as auth_routes
import eos.routes.booking as booking_routes
import eos.routes.galleries_admin as gallery_routes
import eos.routes.listings as listing_routes
import eos.routes.onboarding_routes as onboarding_routes
import eos.routes.portal as portal_routes
import eos.routes.signup as signup_routes
import eos.routes.site as site_routes
import eos.routes.studio_admin as studio_admin_routes
import eos.routes.uploads as upload_routes
import eos.security as security
import eos.sequences as sequences
import eos.signup_verify as signup_verify
import eos.stripe_checkout as stripe_checkout
import eos.stripe_connect as stripe_connect
import eos.studio as studio
import eos.studio_seed as studio_seed
import eos.tenant as tenant
import eos.users as users
import eos.webhooks as webhooks
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

APEX_ORIGIN = "http://localhost:8410"
STUDIO_SLUG = "beta-smoke"
TENANT_ORIGIN = f"http://{STUDIO_SLUG}.localhost:8410"
OWNER_EMAIL = "owner@beta-smoke.test"
OWNER_PASSWORD = "beta-smoke-pass"
FIRST_REQUEST_KEY = "beta-smoke-first-booking"
REFERRAL_REQUEST_KEY = "beta-smoke-referral-booking"


@pytest.fixture()
def beta_smoke_env(tmp_path, monkeypatch):
    """Fresh hosted-SaaS app with local jobs on and every provider off."""
    values = {
        "EOS_DATA_DIR": str(tmp_path / "data"),
        "EOS_SECRET_KEY": "test-secret-key-32chars-minimum!!",
        "EOS_ADMIN_PASSWORD": "test-admin-pass",
        "EOS_BASE_URL": APEX_ORIGIN,
        "EOS_BASE_DOMAIN": "localhost:8410",
        "EOS_COOKIE_SECURE": "false",
        "EOS_SAAS_MODE": "true",
        "EOS_SIGNUP_ENABLED": "true",
        "EOS_SIGNUP_AUTO_VERIFY_LOCAL": "true",
        "EOS_SIGNUP_INVITE_ONLY": "false",
        "EOS_BILLING_ENFORCE": "false",
        "EOS_DEMO_ENABLED": "false",
        "EOS_EMAIL_PROVIDER": "smtp",
        "EOS_GMAIL_USER": "",
        "EOS_GMAIL_APP_PASSWORD": "",
        "EOS_POSTMARK_API_KEY": "",
        "EOS_POSTMARK_FROM_EMAIL": "",
        "EOS_STRIPE_SECRET_KEY": "",
        "EOS_STRIPE_WEBHOOK_SECRET": "",
        "EOS_STRIPE_PLATFORM_SECRET_KEY": "",
        "EOS_STRIPE_PLATFORM_WEBHOOK_SECRET": "",
        "EOS_STRIPE_PRICE_STARTER": "",
        "EOS_STRIPE_PRICE_PRO": "",
        "EOS_GOOGLE_CLIENT_ID": "",
        "EOS_GOOGLE_CLIENT_SECRET": "",
        "EOS_DROPBOX_APP_KEY": "",
        "EOS_DROPBOX_APP_SECRET": "",
        "EOS_TWILIO_ACCOUNT_SID": "",
        "EOS_TWILIO_AUTH_TOKEN": "",
        "EOS_TWILIO_FROM_NUMBER": "",
        "EOS_S3_BUCKET": "",
        "EOS_S3_ENDPOINT": "",
        "EOS_S3_ACCESS_KEY": "",
        "EOS_S3_SECRET_KEY": "",
        "EOS_MIN_FREE_GB": "0",
        "EOS_JOB_WORKERS": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    for module in (
        config,
        db,
        security,
        tenant,
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
        portal,
        sequences,
        jobs,
        webhooks,
        delivery_notify,
        automations,
        appointments,
        listings,
        galleries,
        commerce,
        onboarding,
        onboarding_wizard,
        auth_routes,
        booking_routes,
        appointment_routes,
        listing_routes,
        gallery_routes,
        upload_routes,
        portal_routes,
        onboarding_routes,
        signup_routes,
        site_routes,
        studio_admin_routes,
        main,
    ):
        importlib.reload(module)

    config.ensure_dirs()
    db.migrate()
    tenant.set_studio("default")

    def reject_provider(*_args, **_kwargs):
        raise AssertionError("external provider access is forbidden in beta smoke")

    monkeypatch.setattr(mailer, "send_platform", reject_provider)
    monkeypatch.setattr(mailer, "send_for_studio", reject_provider)
    monkeypatch.setattr(
        webhooks.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(webhooks, "_post_pinned", reject_provider)
    monkeypatch.setattr(webhooks, "_wake_delivery", lambda _delivery_id: None)

    assert not mailer.configured()
    assert not stripe_checkout.payments_configured()
    assert not platform_billing.is_configured()

    jobs.start()
    try:
        yield SimpleNamespace(app=main.app)
    finally:
        jobs.stop()
        tenant.set_studio("default")


def _tiny_jpeg() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (64, 48), color=(180, 205, 225)).save(stream, format="JPEG")
    return stream.getvalue()


def _slots(page: str) -> list[str]:
    return re.findall(r'<option value="([^"]+)"', page)


def _first_distinct_slot(page: str, excluded: str = "") -> str:
    match = next((slot for slot in _slots(page) if slot != excluded), None)
    assert match, "booking page did not expose a distinct open slot"
    return match


def _href(page: str, label: str) -> str:
    pattern = rf'<a[^>]+href="([^"]+)"[^>]*>{re.escape(label)}</a>'
    match = re.search(pattern, page)
    assert match, f"missing {label!r} link"
    return match.group(1)


def _admin_form(csrf: str, **values: Any) -> dict[str, Any]:
    return {security.CSRF_FORM: csrf, **values}


def _admin_headers(csrf: str | None = None) -> dict[str, str]:
    headers = {
        "origin": TENANT_ORIGIN,
        "sec-fetch-site": "same-origin",
    }
    if csrf:
        headers["x-eos-csrf"] = csrf
    return headers


def _booking_payload(
    *,
    package_id: int,
    slot: str,
    request_key: str,
    name: str,
    email: str,
    address: str,
    promo_code: str = "",
) -> dict[str, str]:
    return {
        "name": name,
        "email": email,
        "phone": "512-555-0142",
        "property_address": address,
        "package_id": str(package_id),
        "scheduled_at": slot,
        "signer_name": name,
        "promo_code": promo_code,
        "request_key": request_key,
    }


async def _wait_for(
    description: str,
    fetch: Callable[[], Any],
    ready: Callable[[Any], bool],
    *,
    timeout: float = 10.0,
) -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fetch()
        if ready(last):
            return last
        await asyncio.sleep(0.05)
    pytest.fail(f"timed out waiting for {description}; last state={last!r}")


def _count(table: str, *, studio_id: str = STUDIO_SLUG) -> int:
    row = db.one(
        f"SELECT COUNT(*) AS n FROM {table} WHERE studio_id=?",
        (studio_id,),
    )
    return int(row["n"])


@pytest.mark.asyncio
async def test_beta_smoke_full_signup_booking_delivery_repeat_referral_journey(
    beta_smoke_env,
):
    transport = ASGITransport(app=beta_smoke_env.app)

    async with AsyncClient(
        transport=transport,
        base_url=APEX_ORIGIN,
    ) as apex:
        signup = await apex.post(
            "/signup",
            data={
                "studio_name": "Beta Smoke Studio",
                "slug": STUDIO_SLUG,
                "owner_name": "Beta Owner",
                "owner_email": OWNER_EMAIL,
                "owner_password": OWNER_PASSWORD,
            },
            follow_redirects=False,
        )

    assert signup.status_code == 303, signup.text
    assert signup.headers["location"] == f"{TENANT_ORIGIN}/admin/login"
    studio_row = db.one(
        """SELECT signup_verified, signup_verify_token, provisioning_status
           FROM studio WHERE id=?""",
        (STUDIO_SLUG,),
    )
    assert dict(studio_row) == {
        "signup_verified": 1,
        "signup_verify_token": None,
        "provisioning_status": "ready",
    }

    async with (
        AsyncClient(transport=transport, base_url=TENANT_ORIGIN) as admin,
        AsyncClient(transport=transport, base_url=TENANT_ORIGIN) as public,
    ):
        login = await admin.post(
            "/admin/login",
            headers=_admin_headers(),
            data={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text
        assert login.headers["location"] == "/admin/onboarding"
        assert admin.cookies.get(security.ADMIN_COOKIE)
        csrf = admin.cookies.get(security.CSRF_COOKIE)
        assert csrf

        launched = await admin.post(
            "/admin/onboarding/quick-launch",
            headers=_admin_headers(),
            data=_admin_form(
                csrf,
                headline="Bright, MLS-ready photography in 24 hours",
                service_area="Austin and surrounding counties",
            ),
            follow_redirects=False,
        )
        assert launched.status_code == 303, launched.text

        configured = await admin.post(
            "/admin/studio",
            headers=_admin_headers(),
            data=_admin_form(
                csrf,
                name="Beta Smoke Studio",
                contact_email=OWNER_EMAIL,
                headline="Bright, MLS-ready photography in 24 hours",
                about="Route-driven beta smoke studio",
                service_area="Austin and surrounding counties",
                published="true",
                booking_enabled="true",
                min_notice_hours="0",
                buffer_minutes="0",
                slot_minutes="90",
                day_start_min="480",
                day_end_min="1080",
                book_weekdays="0,1,2,3,4,5,6",
                pay_to_download="true",
                watermark_until_paid="true",
                auto_deliver_email="true",
                auto_publish_site="true",
            ),
            follow_redirects=False,
        )
        assert configured.status_code == 303, configured.text

        webhook = await admin.post(
            "/admin/studio/webhooks",
            headers=_admin_headers(),
            data=_admin_form(
                csrf,
                label="Offline delivery sink",
                url="https://hooks.invalid/eos-beta-smoke",
                events="listing.delivered",
            ),
            follow_redirects=False,
        )
        assert webhook.status_code == 200, webhook.text
        assert "New webhook signing secret" in webhook.text
        assert "no-store" in webhook.headers.get("cache-control", "")

        # Deliberate fixture seam: keep Stripe entirely out of this journey.
        tenant.set_studio(STUDIO_SLUG)
        package = db.one(
            """SELECT * FROM service_packages
               WHERE studio_id=? AND active=1 ORDER BY position, id LIMIT 1""",
            (STUDIO_SLUG,),
        )
        assert package
        db.run(
            """UPDATE service_packages SET deposit_cents=0
               WHERE id=? AND studio_id=?""",
            (package["id"], STUDIO_SLUG),
        )

        book_page = await public.get("/book")
        assert book_page.status_code == 200, book_page.text
        assert "Serving Austin and surrounding counties" in book_page.text
        first_slot = _first_distinct_slot(book_page.text)
        first_payload = _booking_payload(
            package_id=package["id"],
            slot=first_slot,
            request_key=FIRST_REQUEST_KEY,
            name="Journey Agent",
            email="journey-agent@example.com",
            address="101 Beta Journey Ave, Austin TX",
        )
        first_booking = await public.post(
            "/book",
            data=first_payload,
            follow_redirects=False,
        )
        replay = await public.post(
            "/book",
            data=first_payload,
            follow_redirects=False,
        )
        assert first_booking.status_code == 303, first_booking.text
        assert replay.status_code == 303, replay.text
        assert replay.headers["location"] == first_booking.headers["location"]

        confirmation = await public.get(first_booking.headers["location"])
        assert confirmation.status_code == 200
        assert "You're booked!" in confirmation.text
        assert "101 Beta Journey Ave" in confirmation.text

        first_inquiry = db.one(
            """SELECT * FROM inquiries
               WHERE studio_id=? AND request_key=?""",
            (STUDIO_SLUG, FIRST_REQUEST_KEY),
        )
        assert first_inquiry["status"] == "confirmed"
        assert first_inquiry["deposit_cents"] == 0
        assert first_inquiry["invoice_id"] is None
        assert _count("inquiries") == 1
        assert _count("listings") == 1
        assert _count("appointments") == 1
        assert _count("invoices") == 0

        appointment = db.one(
            """SELECT * FROM appointments
               WHERE id=? AND studio_id=?""",
            (first_inquiry["appointment_id"], STUDIO_SLUG),
        )
        completed = await admin.post(
            f"/admin/appointments/{appointment['id']}",
            headers=_admin_headers(),
            data=_admin_form(
                csrf,
                title=appointment["title"],
                kind=appointment["kind"],
                status="completed",
                starts_at="",
                location=appointment["location"],
                assigned_user_id="",
                listing_id=str(appointment["listing_id"]),
                client_id=str(appointment["client_id"]),
                view="month",
                date=first_slot[:10],
                photographer="",
            ),
            follow_redirects=False,
        )
        assert completed.status_code == 303, completed.text
        assert (
            db.one(
                """SELECT status FROM appointments
               WHERE id=? AND studio_id=?""",
                (appointment["id"], STUDIO_SLUG),
            )["status"]
            == "completed"
        )

        gallery_created = await admin.post(
            f"/admin/listings/{first_inquiry['listing_id']}/gallery",
            headers=_admin_headers(),
            data=_admin_form(csrf),
            follow_redirects=False,
        )
        assert gallery_created.status_code == 303, gallery_created.text
        gallery_match = re.fullmatch(
            r"/admin/galleries/(\d+)",
            gallery_created.headers["location"],
        )
        assert gallery_match
        gallery_id = int(gallery_match.group(1))

        upload = await admin.post(
            f"/admin/galleries/{gallery_id}/upload",
            headers=_admin_headers(csrf),
            files={"files": ("front-exterior.jpg", _tiny_jpeg(), "image/jpeg")},
        )
        assert upload.status_code == 200, upload.text
        assert upload.json() == {"accepted": 1, "rejected": []}

        asset = await _wait_for(
            "uploaded image derivatives",
            lambda: db.one(
                """SELECT * FROM assets
                   WHERE gallery_id=? ORDER BY id LIMIT 1""",
                (gallery_id,),
            ),
            lambda row: bool(row and row["status"] in {"ready", "failed"}),
        )
        assert asset["status"] == "ready", dict(asset)
        assert asset["width"] == 64
        assert asset["height"] == 48

        ready_page = await admin.get(f"/admin/galleries/{gallery_id}")
        assert ready_page.status_code == 200
        assert "Ready to publish and deliver." in ready_page.text

        gallery = db.one(
            "SELECT * FROM galleries WHERE id=? AND studio_id=?",
            (gallery_id, STUDIO_SLUG),
        )
        published = await admin.post(
            f"/admin/galleries/{gallery_id}/settings",
            headers=_admin_headers(),
            data=_admin_form(
                csrf,
                title=gallery["title"],
                client_name=gallery["client_name"],
                pin=gallery["pin"],
                expires_at="",
                published="true",
                listing_id=str(first_inquiry["listing_id"]),
            ),
            follow_redirects=False,
        )
        assert published.status_code == 303, published.text

        finished_jobs = await _wait_for(
            "local imaging and exports jobs",
            lambda: [
                dict(row)
                for row in db.all_(
                    "SELECT * FROM jobs ORDER BY id",
                )
            ],
            lambda rows: (
                bool(rows) and all(row["status"] not in {"queued", "running"} for row in rows)
            ),
        )
        assert len(finished_jobs) == 2
        assert {row["kind"] for row in finished_jobs} == {
            "image_derivatives",
            "gallery_exports",
        }
        assert all(row["status"] == "done" for row in finished_jobs)

        gallery = db.one(
            "SELECT * FROM galleries WHERE id=? AND studio_id=?",
            (gallery_id, STUDIO_SLUG),
        )
        delivered_listing = db.one(
            "SELECT * FROM listings WHERE id=? AND studio_id=?",
            (first_inquiry["listing_id"], STUDIO_SLUG),
        )
        first_client = db.one(
            "SELECT * FROM clients WHERE id=? AND studio_id=?",
            (first_inquiry["client_id"], STUDIO_SLUG),
        )
        assert gallery["published"] == 1
        assert delivered_listing["status"] == "delivered"
        assert delivered_listing["delivered_at"]
        assert delivered_listing["site_published"] == 1
        assert first_client["portal_token"]

        notification = db.one(
            """SELECT * FROM delivery_notifications
               WHERE studio_id=? AND gallery_id=?""",
            (STUDIO_SLUG, gallery_id),
        )
        assert notification["status"] == "pending"
        assert notification["attempts"] == 0
        assert notification["claimed_at"] is None

        webhook_intents = db.all_(
            """SELECT * FROM webhook_deliveries
               WHERE studio_id=? ORDER BY id""",
            (STUDIO_SLUG,),
        )
        assert len(webhook_intents) == 1
        assert webhook_intents[0]["event"] == "listing.delivered"
        assert webhook_intents[0]["status"] == "pending"
        assert webhook_intents[0]["attempts"] == 0
        assert webhook_intents[0]["claimed_at"] is None
        assert not db.one("SELECT 1 AS x FROM emails_log LIMIT 1")
        assert not db.one("SELECT 1 AS x FROM stripe_event_receipts LIMIT 1")

        portal_page = await public.get(f"/portal/{first_client['portal_token']}")
        assert portal_page.status_code == 200, portal_page.text
        assert delivered_listing["title"] in portal_page.text
        assert f"{TENANT_ORIGIN}/g/{gallery['slug']}" in portal_page.text

        referral = db.one(
            """SELECT * FROM referral_codes
               WHERE studio_id=? AND referrer_client_id=?""",
            (STUDIO_SLUG, first_client["id"]),
        )
        assert referral
        rebook_url = _href(portal_page.text, "Book your next listing")
        referral_url = f"{TENANT_ORIGIN}/r/{referral['code']}"
        assert rebook_url == (f"{TENANT_ORIGIN}/book?returning={first_client['portal_token']}")
        assert referral_url in portal_page.text

        rebook_page = await public.get(rebook_url)
        assert rebook_page.status_code == 200
        assert 'name="name" value="Journey Agent"' in rebook_page.text
        assert 'name="email" type="email" value="journey-agent@example.com"' in rebook_page.text

        referral_redirect = await public.get(
            referral_url,
            follow_redirects=False,
        )
        assert referral_redirect.status_code == 303
        assert referral_redirect.headers["location"] == (f"/book?ref={referral['code']}")
        referral_page = await public.get(referral_redirect.headers["location"])
        assert referral_page.status_code == 200
        assert f"Referral code <strong>{referral['code']}</strong> applied" in (referral_page.text)
        assert f'name="promo_code" value="{referral["code"]}"' in (referral_page.text)

        referral_slot = _first_distinct_slot(
            referral_page.text,
            excluded=first_slot,
        )
        second_booking = await public.post(
            "/book",
            data=_booking_payload(
                package_id=package["id"],
                slot=referral_slot,
                request_key=REFERRAL_REQUEST_KEY,
                name="Referred Agent",
                email="referred-agent@example.com",
                address="202 Referral Road, Austin TX",
                promo_code=referral["code"],
            ),
            follow_redirects=False,
        )
        assert second_booking.status_code == 303, second_booking.text
        second_confirmation = await public.get(second_booking.headers["location"])
        assert second_confirmation.status_code == 200
        assert "You're booked!" in second_confirmation.text

    redemption = db.one(
        """SELECT rr.*, i.request_key, i.scheduled_at
           FROM referral_redemptions rr
           JOIN inquiries i
             ON i.id=rr.inquiry_id AND i.studio_id=rr.studio_id
           WHERE rr.studio_id=?""",
        (STUDIO_SLUG,),
    )
    assert redemption["referral_id"] == referral["id"]
    assert redemption["referrer_client_id"] == first_client["id"]
    assert redemption["request_key"] == REFERRAL_REQUEST_KEY
    assert redemption["scheduled_at"] == referral_slot
    assert redemption["scheduled_at"] != first_slot
    assert (
        db.one(
            """SELECT uses FROM referral_codes
           WHERE id=? AND studio_id=?""",
            (referral["id"], STUDIO_SLUG),
        )["uses"]
        == 1
    )

    assert {
        table: _count(table)
        for table in (
            "clients",
            "inquiries",
            "listings",
            "appointments",
            "galleries",
            "referral_codes",
            "referral_redemptions",
            "delivery_notifications",
            "webhook_deliveries",
        )
    } == {
        "clients": 2,
        "inquiries": 2,
        "listings": 2,
        "appointments": 2,
        "galleries": 1,
        "referral_codes": 1,
        "referral_redemptions": 1,
        "delivery_notifications": 1,
        "webhook_deliveries": 1,
    }
    assert _count("invoices") == 0

    sequence_runs = db.all_(
        """SELECT status, attempts FROM email_sequence_runs
           WHERE studio_id=? ORDER BY id""",
        (STUDIO_SLUG,),
    )
    assert len(sequence_runs) == 3
    assert all(row["status"] == "scheduled" and row["attempts"] == 0 for row in sequence_runs)

    for table in (
        "users",
        "clients",
        "inquiries",
        "listings",
        "appointments",
        "galleries",
        "email_sequence_runs",
        "webhook_deliveries",
        "referral_codes",
        "referral_redemptions",
        "delivery_notifications",
    ):
        leaked = db.one(
            f"""SELECT COUNT(*) AS n FROM {table}
                WHERE studio_id<>?""",
            (STUDIO_SLUG,),
        )
        assert leaked["n"] == 0, f"{table} contains cross-tenant records"
