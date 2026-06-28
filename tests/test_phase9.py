"""Phase 9 — ops reports, kanban, brokerage billing, team assignment."""

import importlib

import pytest
from httpx import ASGITransport, AsyncClient

import eos.brokerage as brokerage
import eos.config as config
import eos.db as db
import eos.invoices as invoices
import eos.jobs as jobs
import eos.listings as listings
import eos.main as main
import eos.reports as reports


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    for mod in (config, db, jobs, brokerage, invoices, listings, reports, main):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    yield main.app
    jobs.stop()


@pytest.mark.asyncio
async def test_reports_dashboard_shows_revenue(app_env):
    lid = db.run("INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Rev Test', 'delivered')")
    db.run(
        """INSERT INTO invoices (studio_id, listing_id, slug, title, amount_cents, status, paid_at)
           VALUES ('default', ?, 'revslug', 'Paid', 25000, 'paid', datetime('now'))""",
        (lid,),
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post("/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False)
        cookie = login.headers["set-cookie"]
        r = await client.get("/admin/reports", headers={"cookie": cookie})
        assert r.status_code == 200
        assert "250" in r.text
        assert "Top agents" in r.text


@pytest.mark.asyncio
async def test_kanban_lists_pipeline_columns(app_env):
    db.run("INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Kanban Lead', 'lead')")
    db.run("INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Kanban Booked', 'booked')")

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post("/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False)
        cookie = login.headers["set-cookie"]
        r = await client.get("/admin/kanban", headers={"cookie": cookie})
        assert r.status_code == 200
        assert "Kanban Lead" in r.text
        assert "Kanban Booked" in r.text
        assert "Advance" in r.text


def test_brokerage_invoice_bill_to_parent(app_env):
    broker_id = db.run(
        "INSERT INTO clients (studio_id, name, client_type, portal_token) VALUES ('default', 'Big Broker', 'brokerage', 'brk-tok')",
    )
    agent_id = db.run(
        "INSERT INTO clients (studio_id, parent_id, name, client_type, portal_token) VALUES ('default', ?, 'Agent Kay', 'agent', 'ag-tok')",
        (broker_id,),
    )
    lid = db.run(
        "INSERT INTO listings (studio_id, client_id, title, status) VALUES ('default', ?, 'Broker Listing', 'delivered')",
        (agent_id,),
    )
    bill_to, agent = brokerage.resolve_billing(agent_id)
    assert bill_to == broker_id
    assert agent == agent_id

    iid = invoices.create_invoice(lid, title="Broker bill", amount_cents=19900, client_id=agent_id)
    inv = invoices.get_invoice(iid)
    assert inv["bill_to_client_id"] == broker_id
    assert inv["agent_client_id"] == agent_id


@pytest.mark.asyncio
async def test_listing_advance_via_kanban(app_env):
    lid = db.run("INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Advance Me', 'lead')")

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post("/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False)
        cookie = login.headers["set-cookie"]
        r = await client.post(f"/admin/listings/{lid}/advance", headers={"cookie": cookie}, follow_redirects=False)
        assert r.status_code == 303
    row = listings.get_listing(lid)
    assert row["status"] == "booked"