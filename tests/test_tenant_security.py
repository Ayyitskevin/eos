"""Tenant isolation and auth hardening tests."""

import importlib
import socket
from unittest.mock import patch

import eos.api_tokens as api_tokens
import eos.config as config
import eos.db as db
import eos.delivery_notify as delivery_notify
import eos.drive_time as drive_time
import eos.jobs as jobs
import eos.mailer as mailer
import eos.main as main
import eos.onboarding as onboarding
import eos.portal as portal
import eos.reschedule as reschedule
import eos.secret_store as secret_store
import eos.security as security
import eos.sequences as sequences
import eos.tenant as tenant
import eos.users as users
import eos.webhooks as webhooks
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("EOS_SIGNUP_AUTO_VERIFY_LOCAL", "true")
    monkeypatch.setenv("EOS_BASE_DOMAIN", "eos.test")
    monkeypatch.setenv("EOS_SAAS_MODE", "true")
    for mod in (
        config,
        api_tokens,
        db,
        delivery_notify,
        drive_time,
        jobs,
        security,
        tenant,
        mailer,
        sequences,
        onboarding,
        portal,
        reschedule,
        webhooks,
        main,
    ):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    onboarding.create_studio(
        name="Alpha",
        slug="alpha",
        owner_email="a@alpha.test",
        owner_password="alpha-pass-1",
    )
    onboarding.create_studio(
        name="Beta",
        slug="beta",
        owner_email="b@beta.test",
        owner_password="beta-pass-1",
    )
    yield main.app
    jobs.stop()


@pytest.mark.asyncio
async def test_cross_tenant_admin_blocked(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "a@alpha.test", "password": "alpha-pass-1"},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        cookie = login.headers["set-cookie"]
        r = await client.get(
            "/admin",
            headers={"host": "beta.eos.test", "cookie": cookie},
        )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_legacy_admin_disabled_multi_studio(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            "/admin/login",
            data={"password": "test-admin-pass"},
            headers={"host": "eos.test"},
            follow_redirects=False,
        )
        assert r.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("billing_status", "trial_ends_at"),
    [
        ("none", None),
        ("trialing", None),
        ("trialing", "not-a-trial-date"),
    ],
)
async def test_api_reads_and_mutations_fail_closed_for_invalid_billing_state(
    app_env, billing_status, trial_ends_at
):
    tenant.set_studio("alpha")
    _token_id, raw_token = api_tokens.create_token(label="billing-boundary")
    db.run(
        """UPDATE studio SET billing_status=?, trial_ends_at=?
           WHERE id='alpha'""",
        (billing_status, trial_ends_at),
    )
    headers = {
        "host": "alpha.eos.test",
        "authorization": f"Bearer {raw_token}",
    }
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://eos.test") as client:
        read = await client.get("/api/v1/listings", headers=headers)
        mutation = await client.post(
            "/api/v1/listings",
            headers=headers,
            json={"title": "Blocked API mutation"},
        )

    assert read.status_code == 403
    assert mutation.status_code == 403
    assert not db.one(
        """SELECT id FROM listings
           WHERE studio_id='alpha' AND title='Blocked API mutation'"""
    )


@pytest.mark.asyncio
async def test_upload_rejects_other_studio_gallery(app_env):
    tenant.set_studio("alpha")
    gid = db.run(
        "INSERT INTO galleries (studio_id, slug, title, pin, delivery_token) VALUES ('alpha','s1','G','0000','tok')",
    )
    from eos import media_paths

    aid = db.run(
        "INSERT INTO assets (gallery_id, kind, filename, stored, status) "
        "VALUES (?, 'photo', 'private.jpg', 'private.jpg', 'ready')",
        (gid,),
    )
    original = media_paths.gallery_subdir(gid, "original", studio_id="alpha") / "private.jpg"
    original.write_bytes(b"alpha-private-original")
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        anonymous = await client.get(
            f"/admin/galleries/{gid}/media/original/{aid}",
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        assert anonymous.status_code == 303
        login = await client.post(
            "/admin/login",
            data={"email": "b@beta.test", "password": "beta-pass-1"},
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        cookie = login.headers["set-cookie"]
        csrf = client.cookies.get(security.CSRF_COOKIE)
        cross_tenant_media = await client.get(
            f"/admin/galleries/{gid}/media/original/{aid}",
            headers={"host": "beta.eos.test", "cookie": cookie},
            follow_redirects=False,
        )
        assert cross_tenant_media.status_code == 404
        r = await client.post(
            f"/admin/galleries/{gid}/upload",
            files={"files": ("x.jpg", b"fake", "image/jpeg")},
            headers={
                "host": "beta.eos.test",
                "cookie": f"{cookie}; {security.CSRF_COOKIE}={csrf}",
                "x-eos-csrf": csrf,
            },
        )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_admin_post_requires_csrf_with_browser_metadata(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "a@alpha.test", "password": "alpha-pass-1"},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(security.CSRF_COOKIE)
        assert csrf

        blocked = await client.post(
            "/admin/clients",
            data={"name": "Blocked Agent"},
            headers={"host": "alpha.eos.test", "sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert blocked.status_code == 403

        allowed = await client.post(
            "/admin/clients",
            data={"name": "Allowed Agent", security.CSRF_FORM: csrf},
            headers={"host": "alpha.eos.test", "sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert allowed.status_code == 303


@pytest.mark.asyncio
async def test_cross_tenant_client_mutation_blocked(app_env):
    tenant.set_studio("alpha")
    alpha_client = db.run(
        "INSERT INTO clients (studio_id, name, email) VALUES ('alpha', 'Alpha Agent', 'agent@alpha.test')"
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "b@beta.test", "password": "beta-pass-1"},
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(security.CSRF_COOKIE)
        assert csrf
        headers = {"host": "beta.eos.test", "sec-fetch-site": "same-origin"}

        update = await client.post(
            f"/admin/clients/{alpha_client}",
            data={
                "name": "Tampered",
                "client_type": "agent",
                security.CSRF_FORM: csrf,
            },
            headers=headers,
            follow_redirects=False,
        )
        assert update.status_code == 404
        row = db.one("SELECT name FROM clients WHERE id=?", (alpha_client,))
        assert row["name"] == "Alpha Agent"

        create_child = await client.post(
            "/admin/clients",
            data={"name": "Bad Child", "parent_id": str(alpha_client), security.CSRF_FORM: csrf},
            headers=headers,
            follow_redirects=False,
        )
        assert create_child.status_code == 404
        leaked = db.one(
            "SELECT 1 AS x FROM clients WHERE studio_id='beta' AND parent_id=?",
            (alpha_client,),
        )
        assert leaked is None


@pytest.mark.asyncio
async def test_cross_tenant_gallery_section_delete_blocked(app_env):
    tenant.set_studio("alpha")
    gallery_id = db.run(
        """INSERT INTO galleries (studio_id, slug, title, pin, delivery_token)
           VALUES ('alpha', 'alpha-gallery', 'Alpha Gallery', '0000', 'tok-alpha')"""
    )
    section_id = db.run(
        "INSERT INTO sections (gallery_id, name, position) VALUES (?, 'Alpha Section', 0)",
        (gallery_id,),
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "b@beta.test", "password": "beta-pass-1"},
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(security.CSRF_COOKIE)
        assert csrf

        blocked = await client.post(
            f"/admin/galleries/{gallery_id}/sections/{section_id}/delete",
            data={security.CSRF_FORM: csrf},
            headers={"host": "beta.eos.test", "sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert blocked.status_code == 404
        assert db.one("SELECT id FROM sections WHERE id=?", (section_id,)) is not None


@pytest.mark.asyncio
async def test_cross_tenant_gallery_listing_link_blocked(app_env):
    tenant.set_studio("alpha")
    alpha_listing = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('alpha', 'Alpha Listing', 'lead')"
    )
    tenant.set_studio("beta")
    beta_gallery = db.run(
        """INSERT INTO galleries (studio_id, slug, title, pin, delivery_token)
           VALUES ('beta', 'beta-gallery', 'Beta Gallery', '1111', 'tok-beta')"""
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "b@beta.test", "password": "beta-pass-1"},
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(security.CSRF_COOKIE)
        assert csrf

        blocked = await client.post(
            f"/admin/galleries/{beta_gallery}/settings",
            data={
                "title": "Beta Gallery",
                "pin": "1111",
                "client_name": "",
                "expires_at": "",
                "listing_id": str(alpha_listing),
                security.CSRF_FORM: csrf,
            },
            headers={"host": "beta.eos.test", "sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert blocked.status_code == 404
        row = db.one("SELECT listing_id FROM galleries WHERE id=?", (beta_gallery,))
        assert row["listing_id"] is None


@pytest.mark.asyncio
async def test_cross_tenant_proposal_send_rejects_foreign_listing(app_env):
    tenant.set_studio("alpha")
    alpha_listing = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('alpha', 'Alpha Proposal', 'lead')"
    )
    beta_proposal = db.run(
        """INSERT INTO proposals (studio_id, listing_id, slug, title, status)
           VALUES ('beta', ?, 'bad-beta-proposal', 'Bad Proposal', 'draft')""",
        (alpha_listing,),
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "b@beta.test", "password": "beta-pass-1"},
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(security.CSRF_COOKIE)
        assert csrf

        blocked = await client.post(
            f"/admin/proposals/{beta_proposal}/send",
            data={security.CSRF_FORM: csrf},
            headers={"host": "beta.eos.test", "sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert blocked.status_code == 404
        proposal = db.one("SELECT status FROM proposals WHERE id=?", (beta_proposal,))
        listing = db.one("SELECT status FROM listings WHERE id=?", (alpha_listing,))
        assert proposal["status"] == "draft"
        assert listing["status"] == "lead"


@pytest.mark.asyncio
async def test_cross_tenant_contract_public_view_rejects_foreign_listing(app_env):
    tenant.set_studio("alpha")
    alpha_listing = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('alpha', 'Alpha Contract', 'lead')"
    )
    db.run(
        """INSERT INTO contracts
           (studio_id, listing_id, slug, title, body, body_sha256, status)
           VALUES ('beta', ?, 'bad-beta-contract', 'Bad Contract', 'body', '', 'sent')""",
        (alpha_listing,),
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        blocked = await client.get(
            "/c/bad-beta-contract",
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        assert blocked.status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_invoice_send_rejects_foreign_listing(app_env):
    tenant.set_studio("alpha")
    alpha_listing = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('alpha', 'Alpha Invoice', 'lead')"
    )
    beta_invoice = db.run(
        """INSERT INTO invoices (studio_id, listing_id, slug, title, amount_cents, status)
           VALUES ('beta', ?, 'bad-beta-invoice-admin', 'Bad Invoice', 1000, 'draft')""",
        (alpha_listing,),
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "b@beta.test", "password": "beta-pass-1"},
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(security.CSRF_COOKIE)
        assert csrf

        blocked = await client.post(
            f"/admin/invoices/{beta_invoice}/send",
            data={security.CSRF_FORM: csrf},
            headers={"host": "beta.eos.test", "sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert blocked.status_code == 404
        row = db.one("SELECT status FROM invoices WHERE id=?", (beta_invoice,))
        assert row["status"] == "draft"


@pytest.mark.asyncio
async def test_cross_tenant_invoice_public_view_rejects_foreign_client(app_env):
    tenant.set_studio("alpha")
    alpha_client = db.run(
        "INSERT INTO clients (studio_id, name, email) VALUES ('alpha', 'Alpha Buyer', 'buyer@alpha.test')"
    )
    db.run(
        """INSERT INTO invoices (studio_id, client_id, slug, title, amount_cents, status)
           VALUES ('beta', ?, 'bad-beta-invoice-public', 'Bad Invoice', 1000, 'sent')""",
        (alpha_client,),
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        blocked = await client.get(
            "/i/bad-beta-invoice-public",
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        assert blocked.status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_upsell_public_confirm_rejects_foreign_listing(app_env):
    tenant.set_studio("alpha")
    alpha_listing = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('alpha', 'Alpha Upsell', 'delivered')"
    )
    tenant.set_studio("beta")
    db.run(
        """INSERT INTO listing_upsell_orders
           (studio_id, listing_id, addon_ids, amount_cents, token)
           VALUES ('beta', ?, '[]', 5000, 'bad-beta-upsell')""",
        (alpha_listing,),
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        blocked = await client.get(
            "/upsell/bad-beta-upsell",
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        assert blocked.status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_sequence_run_cancel_blocked(app_env):
    tenant.set_studio("alpha")
    seq_id = db.run(
        """INSERT INTO email_sequences
           (studio_id, slug, name, trigger_event, subject, body_template)
           VALUES ('alpha', 'alpha-seq', 'Alpha Seq', 'listing.booked', 'Hi', 'Body')"""
    )
    run_id = db.run(
        """INSERT INTO email_sequence_runs
           (studio_id, sequence_id, to_email, scheduled_at)
           VALUES ('alpha', ?, 'agent@alpha.test', datetime('now', '+1 hour'))""",
        (seq_id,),
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "b@beta.test", "password": "beta-pass-1"},
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(security.CSRF_COOKIE)
        assert csrf

        blocked = await client.post(
            f"/admin/sequences/runs/{run_id}/cancel",
            data={security.CSRF_FORM: csrf},
            headers={"host": "beta.eos.test", "sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert blocked.status_code == 404
        row = db.one("SELECT status FROM email_sequence_runs WHERE id=?", (run_id,))
        assert row["status"] == "scheduled"


def test_sequence_worker_binds_each_run_studio(app_env, monkeypatch):
    monkeypatch.setattr(config, "GMAIL_USER", "test@gmail.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-pass")
    tenant.set_studio("beta")
    client_id = db.run(
        "INSERT INTO clients (studio_id, name, email) VALUES ('beta', 'Beta Agent', 'agent@beta.test')"
    )
    listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status)
           VALUES ('beta', ?, 'Beta Listing', 'booked')""",
        (client_id,),
    )
    seq_id = db.run(
        """INSERT INTO email_sequences
           (studio_id, slug, name, trigger_event, subject, body_template)
           VALUES ('beta', 'beta-seq', 'Beta Seq', 'listing.booked',
                   'Booked {listing_title}', 'Hi {client_first} from {site_name}')"""
    )
    run_id = db.run(
        """INSERT INTO email_sequence_runs
           (studio_id, sequence_id, listing_id, client_id, to_email, scheduled_at)
           VALUES ('beta', ?, ?, ?, 'agent@beta.test', datetime('now', '-1 minute'))""",
        (seq_id, listing_id, client_id),
    )
    tenant.set_studio("default")

    with patch("eos.mailer.send") as send:
        assert sequences.process_due() == 1

    send.assert_called_once()
    assert send.call_args.kwargs["from_name"].startswith("Beta via")
    assert "Beta Listing" in send.call_args.args[1]
    assert "Hi Beta from Beta" in send.call_args.args[2]
    row = db.one("SELECT status FROM email_sequence_runs WHERE id=?", (run_id,))
    assert row["status"] == "sent"
    assert tenant.get_studio_id() == "default"


def test_asset_job_binds_asset_studio(app_env):
    tenant.set_studio("beta")
    gallery_id = db.run(
        """INSERT INTO galleries (studio_id, slug, title, pin, delivery_token)
           VALUES ('beta', 'beta-job-gallery', 'Beta Job Gallery', '1111', 'tok-beta-job')"""
    )
    asset_id = db.run(
        """INSERT INTO assets (gallery_id, kind, filename, stored, status)
           VALUES (?, 'video', 'tour.mp4', 'tour.mp4', 'pending')""",
        (gallery_id,),
    )
    with patch.object(jobs, "_pool", None):
        job_id = jobs.enqueue("video_ready", {"asset_id": asset_id})
    tenant.set_studio("default")

    jobs._execute(job_id)

    row = db.one("SELECT status FROM assets WHERE id=?", (asset_id,))
    assert row["status"] == "ready"
    assert tenant.get_studio_id() == "default"


def test_portal_token_requires_current_studio_client(app_env):
    tenant.set_studio("alpha")
    alpha_client = db.run(
        "INSERT INTO clients (studio_id, name, email) VALUES ('alpha', 'Alpha Agent', 'agent@alpha.test')"
    )

    tenant.set_studio("beta")
    with pytest.raises(HTTPException) as exc:
        portal.ensure_token(alpha_client)

    assert exc.value.status_code == 404
    row = db.one("SELECT portal_token FROM clients WHERE id=?", (alpha_client,))
    assert row["portal_token"] is None


def test_drive_time_listing_update_stops_after_tenant_context_switch(app_env):
    tenant.set_studio("alpha")
    listing_id = db.run(
        """INSERT INTO listings (studio_id, title, address_line1, city, state, zip)
           VALUES ('alpha', 'Alpha Drive', '1 Main St', 'Austin', 'TX', '78701')"""
    )

    def switch_tenant_geocode(**_kwargs):
        tenant.set_studio("beta")
        return (30.2672, -97.7431)

    with patch("eos.drive_time.geocode_address", side_effect=switch_tenant_geocode):
        drive_time.geocode_listing(listing_id)

    row = db.one("SELECT latitude, longitude FROM listings WHERE id=?", (listing_id,))
    assert row["latitude"] is None
    assert row["longitude"] is None


def test_reschedule_confirm_cleanup_stops_after_tenant_context_switch(app_env):
    tenant.set_studio("alpha")
    client_id = db.run(
        "INSERT INTO clients (studio_id, name, email) VALUES ('alpha', 'Alpha Agent', 'agent@alpha.test')"
    )
    appointment_id = db.run(
        """INSERT INTO appointments (studio_id, client_id, title, kind, status, starts_at, token)
           VALUES ('alpha', ?, 'Alpha Shoot', 'shoot', 'proposed', datetime('now', '+1 day'), 'appt-alpha')""",
        (client_id,),
    )
    hold_id = db.run(
        """INSERT INTO appointment_holds
           (studio_id, appointment_id, client_id, starts_at, token, expires_at)
           VALUES ('alpha', ?, ?, datetime('now', '+2 days'), 'hold-alpha', datetime('now', '+15 minutes'))""",
        (appointment_id, client_id),
    )

    def switch_tenant_reschedule(_appointment_id, *, starts_at):
        assert _appointment_id == appointment_id
        assert starts_at
        tenant.set_studio("beta")

    with patch(
        "eos.reschedule.appointments.reschedule_appointment",
        side_effect=switch_tenant_reschedule,
    ):
        assert reschedule.confirm_hold("hold-alpha", client_id=client_id) == appointment_id

    appt = db.one("SELECT status FROM appointments WHERE id=?", (appointment_id,))
    hold = db.one("SELECT id FROM appointment_holds WHERE id=?", (hold_id,))
    assert appt["status"] == "proposed"
    assert hold is not None


def test_delivery_notify_binds_gallery_owner_studio(app_env):
    tenant.set_studio("beta")
    db.run("UPDATE studio_profiles SET auto_deliver_email=1 WHERE studio_id='beta'")
    client_id = db.run(
        "INSERT INTO clients (studio_id, name, email) VALUES ('beta', 'Beta Agent', 'agent@beta.test')"
    )
    listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status)
           VALUES ('beta', ?, 'Beta Delivered', 'delivered')""",
        (client_id,),
    )
    gallery_id = db.run(
        """INSERT INTO galleries
           (studio_id, listing_id, slug, title, pin, delivery_token, published)
           VALUES ('beta', ?, 'beta-delivery', 'Beta Gallery', '1234', 'tok-beta-delivery', 1)""",
        (listing_id,),
    )
    tenant.set_studio("default")

    with (
        patch("eos.delivery_notify.mailer.configured", return_value=True),
        patch("eos.delivery_notify.mailer.send_for_studio") as send,
    ):
        assert delivery_notify.maybe_send_gallery_email(gallery_id) is True

    send.assert_called_once()
    assert send.call_args.args[0] == "agent@beta.test"
    assert "beta.eos.test" in send.call_args.args[2]
    row = db.one(
        """SELECT e.studio_id, e.listing_id
           FROM emails_log e
           JOIN delivery_notifications n
             ON n.id=e.doc_id AND n.studio_id=e.studio_id
           WHERE e.doc_kind='gallery_delivery' AND n.gallery_id=?""",
        (gallery_id,),
    )
    assert row["studio_id"] == "beta"
    assert row["listing_id"] == listing_id
    assert tenant.get_studio_id() == "default"


def test_webhook_post_binds_and_restores_dispatch_studio(app_env):
    tenant.set_studio("default")
    seen_studios: list[str] = []

    def capture_pinned(*_args, **_kwargs):
        seen_studios.append(tenant.get_studio_id())
        return 204

    answers = [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 443),
        )
    ]
    with (
        patch("eos.webhooks.socket.getaddrinfo", return_value=answers),
        patch("eos.webhooks._post_pinned", side_effect=capture_pinned),
    ):
        webhooks._post(
            42,
            "beta",
            "https://hooks.example.test/eos",
            "secret",
            {"event": "listing.delivered", "data": {"listing_id": 1}},
        )

    assert seen_studios == ["beta"]
    row = db.one(
        "SELECT studio_id, status FROM webhook_deliveries WHERE subscription_id=?",
        (42,),
    )
    assert row["studio_id"] == "beta"
    assert row["status"] == "ok"
    assert tenant.get_studio_id() == "default"


def _session_cookie(client) -> str:
    return f"{security.ADMIN_COOKIE}={client.cookies.get(security.ADMIN_COOKIE)}"


@pytest.mark.asyncio
async def test_logout_revokes_session_server_side(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "a@alpha.test", "password": "alpha-pass-1"},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        cookie = _session_cookie(client)
        ok = await client.get("/admin", headers={"host": "alpha.eos.test", "cookie": cookie})
        assert ok.status_code == 200

        await client.post(
            "/admin/logout",
            headers={"host": "alpha.eos.test", "cookie": cookie},
            follow_redirects=False,
        )
        # Replaying the same signed cookie must fail: the server-side row is revoked.
        replay = await client.get(
            "/admin",
            headers={"host": "alpha.eos.test", "cookie": cookie},
            follow_redirects=False,
        )
        assert replay.status_code == 303
        assert replay.headers["location"] == "/admin/login"


@pytest.mark.asyncio
async def test_password_change_revokes_existing_sessions(app_env):
    user = users.get_by_email("a@alpha.test", studio_id="alpha")
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "a@alpha.test", "password": "alpha-pass-1"},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        cookie = _session_cookie(client)

        tenant.set_studio("alpha")
        users.set_password(user["id"], "alpha-pass-rotated")

        replay = await client.get(
            "/admin",
            headers={"host": "alpha.eos.test", "cookie": cookie},
            follow_redirects=False,
        )
        assert replay.status_code == 303
        old_password = await client.post(
            "/admin/login",
            data={"email": "a@alpha.test", "password": "alpha-pass-1"},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        assert old_password.status_code == 401
        new_password = await client.post(
            "/admin/login",
            data={"email": "a@alpha.test", "password": "alpha-pass-rotated"},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        assert new_password.status_code == 303


@pytest.mark.asyncio
async def test_stateless_session_cookie_is_rejected(app_env):
    user = users.get_by_email("a@alpha.test", studio_id="alpha")
    forged = f"{security.ADMIN_COOKIE}={security.sign(f'user:{user["id"]}')}"
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get(
            "/admin",
            headers={"host": "alpha.eos.test", "cookie": forged},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


def test_session_max_age_defaults_to_fourteen_days(app_env):
    assert config.SESSION_MAX_AGE == 60 * 60 * 24 * 14


def test_secret_store_fails_closed_without_key_in_saas_mode(app_env, monkeypatch):
    monkeypatch.setattr(config, "TOKEN_ENCRYPTION_KEY", "")
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        secret_store.encrypt("tok")
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        secret_store.decrypt("tok")


def test_secret_store_rejects_undecryptable_ciphertext_in_saas_mode(app_env):
    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):
        secret_store.decrypt(secret_store.encrypt("real")[:-4] + "XXXX")


def test_secret_store_dev_mode_still_passes_through(app_env, monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "TOKEN_ENCRYPTION_KEY", "")
    assert secret_store.encrypt("plain") == "plain"
    assert secret_store.decrypt("not-a-fernet-token") == "not-a-fernet-token"


def test_saas_startup_refuses_insecure_configuration(app_env, monkeypatch):
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        main.enforce_saas_startup_security()
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    monkeypatch.setattr(config, "TOKEN_ENCRYPTION_KEY", "")
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        main.enforce_saas_startup_security()
    monkeypatch.setattr(config, "TOKEN_ENCRYPTION_KEY", "key")
    main.enforce_saas_startup_security()


def test_solo_startup_skips_saas_security_gate(app_env, monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "TOKEN_ENCRYPTION_KEY", "")
    main.enforce_saas_startup_security()


@pytest.mark.asyncio
async def test_email_send_redirect_is_local_only(app_env, monkeypatch):
    tenant.set_studio("alpha")
    gid = db.run(
        """INSERT INTO galleries (studio_id, slug, title, pin, delivery_token)
           VALUES ('alpha', 'redirect-gallery', 'G', '123456', 'tok-redirect')"""
    )
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send_for_studio", lambda *_args: None)

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "a@alpha.test", "password": "alpha-pass-1"},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(security.CSRF_COOKIE)
        headers = {"host": "alpha.eos.test", "sec-fetch-site": "same-origin"}

        async def send(redirect: str):
            return await client.post(
                f"/admin/email/galleries/{gid}",
                data={
                    "to": "agent@example.com",
                    "subject": "Gallery",
                    "message": "link",
                    "redirect": redirect,
                    security.CSRF_FORM: csrf,
                },
                headers=headers,
                follow_redirects=False,
            )

        absolute = await send("https://evil.example/phish")
        scheme_relative = await send("//evil.example/phish")
        local = await send("/admin/galleries")

    assert absolute.status_code == 303
    assert absolute.headers["location"] == "/admin"
    assert scheme_relative.status_code == 303
    assert scheme_relative.headers["location"] == "/admin"
    assert local.status_code == 303
    assert local.headers["location"] == "/admin/galleries"


@pytest.mark.asyncio
async def test_admin_post_without_fetch_metadata_requires_csrf_token(app_env):
    """A client that omits Sec-Fetch-Site can no longer skip CSRF verification."""
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "a@alpha.test", "password": "alpha-pass-1"},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(security.CSRF_COOKIE)
        assert csrf

        no_token = await client.post(
            "/admin/clients",
            data={"name": "No Metadata Agent"},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        assert no_token.status_code == 403

        with_token = await client.post(
            "/admin/clients",
            data={"name": "Token Agent", security.CSRF_FORM: csrf},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        assert with_token.status_code == 303


@pytest.mark.asyncio
async def test_login_lockout_follows_email_across_ips(app_env):
    """Rotating source IPs must not reset login throttling for an account."""
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        for i in range(config.LOGIN_EMAIL_MAX_FAILS):
            r = await client.post(
                "/admin/login",
                data={"email": "a@alpha.test", "password": "wrong-password"},
                headers={"host": "alpha.eos.test", "x-eos-client-ip": f"203.0.113.{i + 1}"},
            )
            assert r.status_code == 401
        # Even the correct password is refused from a fresh IP while locked.
        locked = await client.post(
            "/admin/login",
            data={"email": "a@alpha.test", "password": "alpha-pass-1"},
            headers={"host": "alpha.eos.test", "x-eos-client-ip": "198.51.100.9"},
        )
        assert locked.status_code == 429


@pytest.mark.asyncio
async def test_api_token_endpoint_rate_limited(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://eos.test") as client:
        headers = {"host": "alpha.eos.test", "authorization": "Bearer eos_garbage_token"}
        for _ in range(config.API_TOKEN_MAX_FAILS):
            r = await client.get("/api/v1/listings", headers=headers)
            assert r.status_code == 401
        limited = await client.get("/api/v1/listings", headers=headers)
        assert limited.status_code == 429


def _tiny_jpeg() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(120, 130, 140)).save(buf, format="JPEG")
    return buf.getvalue()


async def _upload(client, gallery_id: int, csrf: str, filename: str, content: bytes):
    return await client.post(
        f"/admin/galleries/{gallery_id}/upload",
        files={"files": (filename, content, "image/jpeg")},
        headers={
            "host": "alpha.eos.test",
            "cookie": (
                f"{security.ADMIN_COOKIE}={client.cookies.get(security.ADMIN_COOKIE)}; "
                f"{security.CSRF_COOKIE}={csrf}"
            ),
            "x-eos-csrf": csrf,
        },
    )


@pytest.mark.asyncio
async def test_upload_enforces_byte_cap_and_content_sniffing(app_env, monkeypatch):
    tenant.set_studio("alpha")
    gid = db.run(
        """INSERT INTO galleries (studio_id, slug, title, pin, delivery_token)
           VALUES ('alpha', 'upload-guard', 'Upload Guard', '123456', 'tok-upload')"""
    )
    db.run("INSERT INTO sections (gallery_id, name, position) VALUES (?, 'Exterior', 0)", (gid,))
    monkeypatch.setattr(config, "UPLOAD_MAX_BYTES", 128)

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "a@alpha.test", "password": "alpha-pass-1"},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(security.CSRF_COOKIE)

        oversized = await _upload(client, gid, csrf, "big.jpg", _tiny_jpeg())
        assert oversized.status_code == 200
        assert oversized.json() == {"accepted": 0, "rejected": ["big.jpg"]}

        monkeypatch.setattr(config, "UPLOAD_MAX_BYTES", 50 * 1024 * 1024)
        fake = await _upload(client, gid, csrf, "fake.jpg", b"<html>not an image</html>")
        assert fake.status_code == 200
        assert fake.json() == {"accepted": 0, "rejected": ["fake.jpg"]}

        real = await _upload(client, gid, csrf, "real.jpg", _tiny_jpeg())
        assert real.status_code == 200
        assert real.json() == {"accepted": 1, "rejected": []}

    assets = db.all_("SELECT filename, stored FROM assets WHERE gallery_id=?", (gid,))
    assert [row["filename"] for row in assets] == ["real.jpg"]
    from eos import media_paths

    stored_dir = media_paths.gallery_subdir(gid, "original", studio_id="alpha")
    on_disk = {p.name for p in stored_dir.iterdir()}
    assert on_disk == {assets[0]["stored"]}
