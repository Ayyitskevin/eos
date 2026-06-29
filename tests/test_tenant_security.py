"""Tenant isolation and auth hardening tests."""

import importlib
from unittest.mock import patch

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.mailer as mailer
import eos.main as main
import eos.onboarding as onboarding
import eos.security as security
import eos.sequences as sequences
import eos.tenant as tenant
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("EOS_BASE_DOMAIN", "eos.test")
    monkeypatch.setenv("EOS_SAAS_MODE", "true")
    for mod in (config, db, jobs, security, tenant, mailer, sequences, onboarding, main):
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
            follow_redirects=False,
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_upload_rejects_other_studio_gallery(app_env):
    tenant.set_studio("alpha")
    gid = db.run(
        "INSERT INTO galleries (studio_id, slug, title, pin, delivery_token) VALUES ('alpha','s1','G','0000','tok')",
    )
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "b@beta.test", "password": "beta-pass-1"},
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        cookie = login.headers["set-cookie"]
        r = await client.post(
            f"/admin/galleries/{gid}/upload",
            files={"files": ("x.jpg", b"fake", "image/jpeg")},
            headers={"host": "beta.eos.test", "cookie": cookie},
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
