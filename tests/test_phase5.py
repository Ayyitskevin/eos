"""Phase 5 — operator auth, email drip sequences, bootstrap."""

import importlib
from unittest.mock import patch

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.main as main
import eos.sequences as sequences
import eos.users as users
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_GMAIL_USER", "test@gmail.com")
    monkeypatch.setenv("EOS_GMAIL_APP_PASSWORD", "app-pass")
    monkeypatch.setenv("EOS_BOOTSTRAP_EMAIL", "owner@studio.test")
    for mod in (config, db, jobs, users, sequences, main):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    from eos import bootstrap

    importlib.reload(bootstrap)
    bootstrap.maybe_bootstrap()
    jobs.start()
    yield main.app
    jobs.stop()


async def _admin_cookie(client):
    login = await client.post(
        "/admin/login",
        data={"password": "test-admin-pass"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    return login.headers["set-cookie"]


@pytest.mark.asyncio
async def test_bootstrap_owner_and_user_login(app_env):
    owner = users.get_by_email("owner@studio.test")
    assert owner is not None
    assert owner["role"] == "owner"

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        bad = await client.post(
            "/admin/login",
            data={"email": "owner@studio.test", "password": "wrong"},
            follow_redirects=False,
        )
        assert bad.status_code == 401

        ok = await client.post(
            "/admin/login",
            data={"email": "owner@studio.test", "password": "test-admin-pass"},
            follow_redirects=False,
        )
        assert ok.status_code == 303
        assert "eos_admin=" in ok.headers["set-cookie"]

        page = await client.get("/admin", headers={"cookie": ok.headers["set-cookie"]})
        assert page.status_code == 200


@pytest.mark.asyncio
async def test_sequence_trigger_and_send(app_env):
    cid = db.run(
        "INSERT INTO clients (studio_id, name, email) VALUES ('default', 'Jane Agent', 'jane@example.com')",
    )
    lid = db.run(
        "INSERT INTO listings (studio_id, client_id, title, status) VALUES ('default', ?, '55 Elm', 'lead')",
        (cid,),
    )
    db.run(
        """INSERT INTO questionnaires (studio_id, listing_id, token, status)
           VALUES ('default', ?, 'tok-test', 'pending')""",
        (lid,),
    )

    with patch("eos.mailer.send") as send:
        n = sequences.trigger("listing.booked", lid)
        assert n == 1
        run = db.one("SELECT * FROM email_sequence_runs WHERE listing_id=?", (lid,))
        assert run["status"] == "scheduled"
        assert run["to_email"] == "jane@example.com"

        db.run(
            "UPDATE email_sequence_runs SET scheduled_at=datetime('now','-1 minute') WHERE id=?",
            (run["id"],),
        )
        sent = sequences.process_due()
        assert sent == 1
        send.assert_called_once()
        subject, body = send.call_args[0][1], send.call_args[0][2]
        assert "55 Elm" in subject
        assert "Jane" in body
        assert "/q/tok-test" in body

        updated = db.one("SELECT status FROM email_sequence_runs WHERE id=?", (run["id"],))
        assert updated["status"] == "sent"


@pytest.mark.asyncio
async def test_sequences_admin_page(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        cookie = await _admin_cookie(client)
        page = await client.get("/admin/sequences", headers={"cookie": cookie})
        assert page.status_code == 200
        assert "Booking confirmation" in page.text
        assert "listing.booked" in page.text
