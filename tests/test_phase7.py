"""Phase 7 — agent portal, pay-to-download, listing embeds."""

import importlib

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
    for mod in (config, db, jobs, paywall, portal, main):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    import eos.studio as studio

    importlib.reload(studio)
    studio.update_profile(pay_to_download=True, watermark_until_paid=True)
    jobs.start()
    yield main.app
    jobs.stop()


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
