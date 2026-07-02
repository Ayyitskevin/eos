"""Phase 9 — ops reports, kanban, brokerage billing, team assignment."""

import importlib

import eos.brokerage as brokerage
import eos.brokerage_reports as brokerage_reports
import eos.config as config
import eos.db as db
import eos.invoices as invoices
import eos.jobs as jobs
import eos.listings as listings
import eos.main as main
import eos.reports as reports
import eos.reports_export as reports_export
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    for mod in (
        config,
        db,
        jobs,
        brokerage,
        brokerage_reports,
        invoices,
        listings,
        reports,
        reports_export,
        main,
    ):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    yield main.app
    jobs.stop()


def _seed_repeat_agent_revenue():
    broker_id = db.run(
        """INSERT INTO clients (studio_id, name, client_type, portal_token)
           VALUES ('default', 'Big Broker', 'brokerage', 'brk-tok')""",
    )
    agent_id = db.run(
        """INSERT INTO clients (studio_id, parent_id, name, client_type, company, portal_token)
           VALUES ('default', ?, 'Repeat Agent', 'agent', 'Acme Realty', 'rep-tok')""",
        (broker_id,),
    )
    one_time_id = db.run(
        """INSERT INTO clients (studio_id, name, client_type, company, portal_token)
           VALUES ('default', 'One Time Agent', 'agent', 'Acme Realty', 'one-tok')""",
    )
    first_listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, created_at)
           VALUES ('default', ?, 'Repeat First', 'delivered', '2026-01-01T10:00:00')""",
        (agent_id,),
    )
    second_listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, created_at)
           VALUES ('default', ?, 'Repeat Second', 'delivered', '2026-02-01T10:00:00')""",
        (agent_id,),
    )
    one_time_listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, created_at)
           VALUES ('default', ?, 'One Time Listing', 'delivered', '2026-03-01T10:00:00')""",
        (one_time_id,),
    )

    first_invoice_id = invoices.create_invoice(
        first_listing_id, title="Repeat first", amount_cents=20000, client_id=agent_id
    )
    second_invoice_id = invoices.create_invoice(
        second_listing_id, title="Repeat second", amount_cents=30000, client_id=agent_id
    )
    one_time_invoice_id = invoices.create_invoice(
        one_time_listing_id, title="One time", amount_cents=90000, client_id=one_time_id
    )
    open_invoice_id = invoices.create_invoice(
        second_listing_id, title="Repeat add-on", amount_cents=5000, client_id=agent_id
    )

    for invoice_id in (first_invoice_id, second_invoice_id, one_time_invoice_id):
        invoices.mark_paid(invoice_id)
    invoices.mark_sent(open_invoice_id)
    return agent_id


def _seed_brokerage_account():
    broker_id = db.run(
        """INSERT INTO clients (studio_id, name, client_type, portal_token)
           VALUES ('default', 'Big Broker', 'brokerage', 'brk-dash')""",
    )
    first_agent_id = db.run(
        """INSERT INTO clients (studio_id, parent_id, name, client_type, company, portal_token)
           VALUES ('default', ?, 'Agent Kay', 'agent', 'Big Broker', 'kay-dash')""",
        (broker_id,),
    )
    second_agent_id = db.run(
        """INSERT INTO clients (studio_id, parent_id, name, client_type, company, portal_token)
           VALUES ('default', ?, 'Agent Jay', 'agent', 'Big Broker', 'jay-dash')""",
        (broker_id,),
    )
    first_listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, created_at)
           VALUES ('default', ?, 'Maple Portfolio', 'delivered', '2026-01-03T09:00:00')""",
        (first_agent_id,),
    )
    second_listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, created_at)
           VALUES ('default', ?, 'Cedar Portfolio', 'booked', '2026-02-04T09:00:00')""",
        (second_agent_id,),
    )

    first_invoice_id = invoices.create_invoice(
        first_listing_id, title="Maple photos", amount_cents=40000, client_id=first_agent_id
    )
    second_invoice_id = invoices.create_invoice(
        second_listing_id, title="Cedar photos", amount_cents=15000, client_id=second_agent_id
    )
    open_invoice_id = invoices.create_invoice(
        second_listing_id, title="Cedar rush", amount_cents=25000, client_id=second_agent_id
    )

    invoices.mark_paid(first_invoice_id)
    invoices.mark_paid(second_invoice_id)
    invoices.mark_sent(open_invoice_id)
    return broker_id


def _seed_other_studio_brokerage():
    db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other Studio', 'other')")
    broker_id = db.run(
        """INSERT INTO clients (studio_id, name, client_type, portal_token)
           VALUES ('other', 'Other Broker', 'brokerage', 'other-brk')""",
    )
    agent_id = db.run(
        """INSERT INTO clients (studio_id, parent_id, name, client_type, portal_token)
           VALUES ('other', ?, 'Other Agent', 'agent', 'other-agent')""",
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
           VALUES ('other', ?, ?, ?, ?, 'other-paid', 'Other paid', 99000, 'paid', '2026-03-02T09:00:00')""",
        (listing_id, agent_id, broker_id, agent_id),
    )


@pytest.mark.asyncio
async def test_reports_dashboard_shows_revenue(app_env):
    lid = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Rev Test', 'delivered')"
    )
    db.run(
        """INSERT INTO invoices (studio_id, listing_id, slug, title, amount_cents, status, paid_at)
           VALUES ('default', ?, 'revslug', 'Paid', 25000, 'paid', datetime('now'))""",
        (lid,),
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]
        r = await client.get("/admin/reports", headers={"cookie": cookie})
        assert r.status_code == 200
        assert "250" in r.text
        assert "Top agents" in r.text


def test_repeat_agent_revenue_report_and_csv(app_env):
    agent_id = _seed_repeat_agent_revenue()

    rows = reports.repeat_agent_revenue()
    assert len(rows) == 1
    assert rows[0]["id"] == agent_id
    assert rows[0]["name"] == "Repeat Agent"
    assert rows[0]["company"] == "Acme Realty"
    assert rows[0]["brokerage_name"] == "Big Broker"
    assert rows[0]["n_listings"] == 2
    assert rows[0]["n_paid_listings"] == 2
    assert rows[0]["paid_cents"] == 50000
    assert rows[0]["open_cents"] == 5000
    assert rows[0]["avg_listing_value_cents"] == 25000

    summary = reports.repeat_agent_summary()
    assert summary["repeat_agent_count"] == 1
    assert summary["paid_cents"] == 50000
    assert summary["avg_listing_value_display"] == "$250"

    body = reports_export.repeat_agents_csv()
    assert "agent,company,brokerage,listings,paid_listings,paid_cents" in body
    assert "Repeat Agent,Acme Realty,Big Broker,2,2,50000,5000,25000,2026-02-01" in body
    assert "One Time Agent" not in body


@pytest.mark.asyncio
async def test_reports_dashboard_shows_repeat_agent_revenue(app_env):
    _seed_repeat_agent_revenue()

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]
        r = await client.get("/admin/reports", headers={"cookie": cookie})
        assert r.status_code == 200
        assert "Repeat agent revenue" in r.text
        assert "Repeat Agent" in r.text
        assert "Big Broker" in r.text
        assert "$500" in r.text

        export = await client.get("/admin/reports/repeat-agents.csv", headers={"cookie": cookie})
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/csv")
        assert "eos-repeat-agents.csv" in export.headers["content-disposition"]
        assert "Repeat Agent,Acme Realty,Big Broker" in export.text


def test_brokerage_accounts_roll_up_values_and_isolate_studios(app_env):
    broker_id = _seed_brokerage_account()
    _seed_other_studio_brokerage()

    rows = brokerage_reports.brokerage_accounts()
    assert len(rows) == 1
    assert rows[0]["id"] == broker_id
    assert rows[0]["name"] == "Big Broker"
    assert rows[0]["n_agents"] == 2
    assert rows[0]["n_listings"] == 2
    assert rows[0]["n_paid_invoices"] == 2
    assert rows[0]["n_open_invoices"] == 1
    assert rows[0]["paid_cents"] == 55000
    assert rows[0]["open_cents"] == 25000
    assert rows[0]["portfolio_display"] == "$800"
    assert [a["name"] for a in rows[0]["top_agents"]] == ["Agent Kay", "Agent Jay"]
    assert rows[0]["recent_listings"][0]["title"] == "Cedar Portfolio"

    summary = brokerage_reports.brokerage_summary()
    assert summary["n_brokerages"] == 1
    assert summary["n_agents"] == 2
    assert summary["paid_display"] == "$550"
    assert summary["open_display"] == "$250"

    body = brokerage_reports.brokerage_accounts_csv()
    assert "brokerage,company,agents,listings,paid_cents,open_cents" in body
    assert "Big Broker,,2,2,55000,25000,2,1,2026-02-04" in body
    assert "Other Broker" not in body


@pytest.mark.asyncio
async def test_brokerage_dashboard_and_csv_routes(app_env):
    _seed_brokerage_account()

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]
        r = await client.get("/admin/brokerages", headers={"cookie": cookie})
        assert r.status_code == 200
        assert "Brokerage accounts" in r.text
        assert "Big Broker" in r.text
        assert "$550 paid" in r.text
        assert "$250 open" in r.text
        assert "Agent Kay" in r.text
        assert "Cedar Portfolio" in r.text

        export = await client.get("/admin/brokerages.csv", headers={"cookie": cookie})
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/csv")
        assert "eos-brokerages.csv" in export.headers["content-disposition"]
        assert "Big Broker,,2,2,55000,25000" in export.text


@pytest.mark.asyncio
async def test_kanban_lists_pipeline_columns(app_env):
    db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Kanban Lead', 'lead')"
    )
    db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Kanban Booked', 'booked')"
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
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
    lid = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Advance Me', 'lead')"
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]
        r = await client.post(
            f"/admin/listings/{lid}/advance", headers={"cookie": cookie}, follow_redirects=False
        )
        assert r.status_code == 303
    row = listings.get_listing(lid)
    assert row["status"] == "booked"
