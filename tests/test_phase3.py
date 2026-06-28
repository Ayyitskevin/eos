"""Phase 3 — proposals, contracts, section reorder."""

import importlib
import json

import pytest
from httpx import ASGITransport, AsyncClient

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.main as main


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    for mod in (config, db, jobs, main):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    yield main.app
    jobs.stop()


@pytest.mark.asyncio
async def test_proposal_accept_flow(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post("/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False)
        cookie = login.headers["set-cookie"]

        lid = db.run("INSERT INTO listings (studio_id, title, status) VALUES ('default', '456 Elm', 'lead')")
        create = await client.post(
            f"/admin/listings/{lid}/proposals",
            data={"preset": "blank"},
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        pid = int(create.headers["location"].rstrip("/").split("/")[-1])
        slug = db.one("SELECT slug FROM proposals WHERE id=?", (pid,))["slug"]

        send = await client.post(f"/admin/proposals/{pid}/send", headers={"cookie": cookie}, follow_redirects=False)
        assert send.status_code == 303

        page = await client.get(f"/p/{slug}")
        assert page.status_code == 200
        assert "Accept proposal" in page.text

        accept = await client.post(f"/p/{slug}/accept", follow_redirects=False)
        assert accept.status_code == 303

        prop = db.one("SELECT status FROM proposals WHERE id=?", (pid,))
        assert prop["status"] == "accepted"
        listing = db.one("SELECT status FROM listings WHERE id=?", (lid,))
        assert listing["status"] == "booked"


@pytest.mark.asyncio
async def test_contract_sign_flow(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        lid = db.run("INSERT INTO listings (studio_id, title) VALUES ('default', '789 Pine')")
        from eos import contracts
        cid = contracts.create_contract(lid)
        contracts.mark_sent(cid)
        slug = db.one("SELECT slug FROM contracts WHERE id=?", (cid,))["slug"]

        page = await client.get(f"/c/{slug}")
        assert page.status_code == 200
        assert "Sign agreement" in page.text

        sign = await client.post(f"/c/{slug}/sign", data={"signer_name": "Jane Agent"}, follow_redirects=False)
        assert sign.status_code == 303
        row = db.one("SELECT status, signer_name FROM contracts WHERE id=?", (cid,))
        assert row["status"] == "signed"
        assert row["signer_name"] == "Jane Agent"


@pytest.mark.asyncio
async def test_section_reorder(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post("/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False)
        cookie = login.headers["set-cookie"]

        gid = db.run(
            """INSERT INTO galleries (studio_id, slug, title, pin, delivery_token)
               VALUES ('default', 'reordertest1234', 'T', '1111', 'tok')""",
        )
        s1 = db.run("INSERT INTO sections (gallery_id, name, position) VALUES (?, 'A', 0)", (gid,))
        s2 = db.run("INSERT INTO sections (gallery_id, name, position) VALUES (?, 'B', 1)", (gid,))

        await client.post(
            f"/admin/galleries/{gid}/sections/{s2}/move",
            data={"dir": "up"},
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        rows = db.all_("SELECT id FROM sections WHERE gallery_id=? ORDER BY position", (gid,))
        assert rows[0]["id"] == s2
        assert rows[1]["id"] == s1