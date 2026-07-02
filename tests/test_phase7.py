"""Phase 7 — agent portal, pay-to-download, listing embeds."""

import importlib

import eos.brokerage_portal as brokerage_portal
import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.main as main
import eos.paywall as paywall
import eos.portal as portal
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    for mod in (config, db, jobs, paywall, portal, brokerage_portal, main):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    import eos.studio as studio

    importlib.reload(studio)
    studio.update_profile(pay_to_download=True, watermark_until_paid=True)
    jobs.start()
    yield main.app
    jobs.stop()


def _seed_brokerage_portal():
    broker_id = db.run(
        """INSERT INTO clients (studio_id, name, client_type, portal_token)
           VALUES ('default', 'Portal Brokerage', 'brokerage', 'brokerage-portal-7')""",
    )
    agent_one_id = db.run(
        """INSERT INTO clients (studio_id, parent_id, name, client_type, portal_token)
           VALUES ('default', ?, 'Agent Kay', 'agent', 'agent-kay-7')""",
        (broker_id,),
    )
    agent_two_id = db.run(
        """INSERT INTO clients (studio_id, parent_id, name, client_type, portal_token)
           VALUES ('default', ?, 'Agent Jay', 'agent', 'agent-jay-7')""",
        (broker_id,),
    )
    first_listing_id = db.run(
        """INSERT INTO listings
           (studio_id, client_id, title, status, address_line1, site_slug, site_published, created_at)
           VALUES ('default', ?, 'Maple Listing', 'delivered', '100 Maple St', 'maple-site', 1, '2026-01-03T09:00:00')""",
        (agent_one_id,),
    )
    second_listing_id = db.run(
        """INSERT INTO listings
           (studio_id, client_id, title, status, address_line1, created_at)
           VALUES ('default', ?, 'Cedar Listing', 'booked', '200 Cedar Ave', '2026-02-04T09:00:00')""",
        (agent_two_id,),
    )
    db.run(
        """INSERT INTO galleries (studio_id, listing_id, slug, title, pin, delivery_token, published)
           VALUES ('default', ?, 'maple-gallery', 'Maple Gallery', '1234', 'maple-delivery', 1)""",
        (first_listing_id,),
    )
    db.run(
        """INSERT INTO invoices
           (studio_id, listing_id, client_id, bill_to_client_id, agent_client_id,
            slug, title, amount_cents, status, paid_at)
           VALUES ('default', ?, ?, ?, ?, 'maple-paid', 'Maple paid', 40000, 'paid', '2026-01-05T09:00:00')""",
        (first_listing_id, agent_one_id, broker_id, agent_one_id),
    )
    db.run(
        """INSERT INTO invoices
           (studio_id, listing_id, client_id, bill_to_client_id, agent_client_id,
            slug, title, amount_cents, status)
           VALUES ('default', ?, ?, ?, ?, 'cedar-open', 'Cedar open', 25000, 'sent')""",
        (second_listing_id, agent_two_id, broker_id, agent_two_id),
    )
    return broker_id


def _seed_other_studio_brokerage_portal():
    db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other Studio', 'other')")
    broker_id = db.run(
        """INSERT INTO clients (studio_id, name, client_type, portal_token)
           VALUES ('other', 'Other Brokerage', 'brokerage', 'other-brokerage-portal')""",
    )
    agent_id = db.run(
        """INSERT INTO clients (studio_id, parent_id, name, client_type, portal_token)
           VALUES ('other', ?, 'Other Agent', 'agent', 'other-agent-portal')""",
        (broker_id,),
    )
    listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, created_at)
           VALUES ('other', ?, 'Other Listing', 'delivered', '2026-03-01T09:00:00')""",
        (agent_id,),
    )
    db.run(
        """INSERT INTO invoices
           (studio_id, listing_id, client_id, bill_to_client_id, agent_client_id,
            slug, title, amount_cents, status, paid_at)
           VALUES ('other', ?, ?, ?, ?, 'other-portal-paid', 'Other paid', 99000, 'paid', '2026-03-02T09:00:00')""",
        (listing_id, agent_id, broker_id, agent_id),
    )


@pytest.mark.asyncio
async def test_paywall_blocks_download(app_env):
    cid = db.run(
        "INSERT INTO clients (studio_id, name, email, portal_token) VALUES ('default', 'Agent', 'a@test.com', 'tok-portal')",
    )
    lid = db.run(
        "INSERT INTO listings (studio_id, client_id, title, status) VALUES ('default', ?, 'Pay Test', 'delivered')",
        (cid,),
    )
    gid = db.run(
        """INSERT INTO galleries (studio_id, listing_id, slug, title, pin, delivery_token, published)
           VALUES ('default', ?, 'paygal', 'Pay Test', '1234', 'dtok', 1)""",
        (lid,),
    )
    db.run(
        """INSERT INTO invoices (studio_id, listing_id, client_id, slug, title, amount_cents, status, invoice_kind)
           VALUES ('default', ?, ?, 'invslug', 'Balance', 17500, 'sent', 'full')""",
        (lid, cid),
    )
    assert paywall.payment_required(lid)

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]
        pin = await client.post("/g/paygal/pin", data={"pin": "1234"}, follow_redirects=False)
        gal_cookie = pin.headers.get("set-cookie", "")
        r = await client.get(
            "/g/paygal/download/zip",
            headers={"cookie": gal_cookie},
            follow_redirects=False,
        )
        assert r.status_code == 402


@pytest.mark.asyncio
async def test_agent_portal_lists_deliveries(app_env):
    cid = db.run(
        "INSERT INTO clients (studio_id, name, email, portal_token) VALUES ('default', 'Portal Agent', 'p@test.com', 'portal-tok-7')",
    )
    lid = db.run(
        "INSERT INTO listings (studio_id, client_id, title, status) VALUES ('default', ?, 'Portal Listing', 'delivered')",
        (cid,),
    )
    db.run(
        """INSERT INTO galleries (studio_id, listing_id, slug, title, pin, delivery_token, published)
           VALUES ('default', ?, 'portgal', 'Portal Listing', '9999', 'dt2', 1)""",
        (lid,),
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/portal/portal-tok-7")
        assert r.status_code == 200
        assert "Portal Listing" in r.text
        assert "/g/portgal" in r.text


def test_brokerage_portal_summary_isolates_other_studios(app_env):
    broker_id = _seed_brokerage_portal()
    _seed_other_studio_brokerage_portal()

    data = brokerage_portal.portal_summary(broker_id)
    assert data["totals"]["n_agents"] == 2
    assert data["totals"]["n_listings"] == 2
    assert data["totals"]["n_delivered"] == 1
    assert data["totals"]["paid_display"] == "$400"
    assert data["totals"]["open_display"] == "$250"
    assert [a["name"] for a in data["agent_activity"]] == ["Agent Kay", "Agent Jay"]
    assert [i["title"] for i in data["open_invoices"]] == ["Cedar open"]
    assert "Other Listing" not in {d["title"] for d in data["deliveries"]}


@pytest.mark.asyncio
async def test_brokerage_portal_shows_account_activity_and_delivery_links(app_env):
    _seed_brokerage_portal()

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/portal/brokerage/brokerage-portal-7")
        assert r.status_code == 200
        assert "Brokerage account" in r.text
        assert "Portal Brokerage" in r.text
        assert "$250" in r.text
        assert "$400" in r.text
        assert "Agent Kay" in r.text
        assert "Agent Jay" in r.text
        assert "Maple Listing" in r.text
        assert "Cedar Listing" in r.text
        assert "/l/maple-site" in r.text
        assert "/g/maple-gallery" in r.text
        assert "PIN 1234" in r.text
        assert "/i/cedar-open" in r.text
        assert "Invoice sent" in r.text


@pytest.mark.asyncio
async def test_listing_media_embed_on_gallery(app_env):
    lid = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Embed Test', 'delivered')"
    )
    db.run(
        """INSERT INTO listing_media (studio_id, listing_id, kind, label, embed_url)
           VALUES ('default', ?, 'youtube', 'Walkthrough', 'https://www.youtube.com/embed/demo')""",
        (lid,),
    )
    db.run(
        """INSERT INTO galleries (studio_id, listing_id, slug, title, pin, delivery_token, published)
           VALUES ('default', ?, 'embedgal', 'Embed Test', '1111', 'dt3', 1)""",
        (lid,),
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        pin = await client.post("/g/embedgal/pin", data={"pin": "1111"}, follow_redirects=False)
        cookie = pin.headers.get("set-cookie", "")
        r = await client.get("/g/embedgal", headers={"cookie": cookie})
        assert r.status_code == 200
        assert "Walkthrough" in r.text
        assert "youtube.com/embed/demo" in r.text
