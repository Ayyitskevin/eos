"""Phase 9 — ops reports, kanban, brokerage billing, team assignment."""

import importlib

import eos.acquisition as acquisition
import eos.brokerage as brokerage
import eos.brokerage_reports as brokerage_reports
import eos.config as config
import eos.db as db
import eos.invoices as invoices
import eos.jobs as jobs
import eos.listings as listings
import eos.main as main
import eos.referrals as referrals
import eos.reports as reports
import eos.reports_export as reports_export
import eos.revenue_optimizer as revenue_optimizer
import pytest
from fastapi import HTTPException
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
        acquisition,
        brokerage,
        brokerage_reports,
        invoices,
        listings,
        reports,
        reports_export,
        revenue_optimizer,
        referrals,
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
    db.run(
        """INSERT INTO referral_codes
           (studio_id, code, credit_cents, referrer_client_id, uses)
           VALUES ('default', 'KAY25', 2500, ?, 1)""",
        (first_agent_id,),
    )
    db.run(
        """INSERT INTO inquiries
           (studio_id, name, email, property_address, status, promo_code,
            listing_id, client_id, total_cents, deposit_cents, created_at)
           VALUES ('default', 'Kay Referral', 'kay-ref@test.com', 'Referral Condo',
                   'confirmed', 'KAY25', ?, ?, 25000, 0, '2026-02-06T09:00:00')""",
        (second_listing_id, second_agent_id),
    )
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


def _seed_revenue_optimizer(*, other_studio: bool = False):
    standard = db.one(
        "SELECT id, name FROM service_packages WHERE studio_id=? AND name='Standard Listing'",
        ("default",),
    )
    premium = db.one(
        "SELECT id, name FROM service_packages WHERE studio_id=? AND name='Premium Listing'",
        ("default",),
    )
    drone = db.one(
        "SELECT id, name FROM service_addons WHERE studio_id=? AND slug='drone'",
        ("default",),
    )
    agent_id = db.run(
        """INSERT INTO clients (studio_id, name, client_type, portal_token)
           VALUES ('default', 'Optimizer Agent', 'agent', 'opt-agent')""",
    )

    standard_listing_id = db.run(
        """INSERT INTO listings
           (studio_id, client_id, title, property_type, status, created_at)
           VALUES ('default', ?, 'Maple Standard', 'residential', 'delivered', '2026-01-03T09:00:00')""",
        (agent_id,),
    )
    premium_listing_id = db.run(
        """INSERT INTO listings
           (studio_id, client_id, title, property_type, status, created_at)
           VALUES ('default', ?, 'Cedar Premium', 'commercial', 'delivered', '2026-02-04T09:00:00')""",
        (agent_id,),
    )
    manual_listing_id = db.run(
        """INSERT INTO listings
           (studio_id, client_id, title, property_type, status, created_at)
           VALUES ('default', ?, 'Manual Standard', 'residential', 'delivered', '2026-03-05T09:00:00')""",
        (agent_id,),
    )

    db.run(
        """INSERT INTO inquiries
           (studio_id, name, email, property_address, status, package_id, addon_ids,
            listing_id, client_id, total_cents, deposit_cents, created_at)
           VALUES ('default', 'Optimizer Agent', 'o@test.com', 'Maple', 'confirmed',
                   ?, '[]', ?, ?, 20000, 0, '2026-01-02T09:00:00')""",
        (standard["id"], standard_listing_id, agent_id),
    )
    db.run(
        """INSERT INTO inquiries
           (studio_id, name, email, property_address, status, package_id, addon_ids,
            listing_id, client_id, total_cents, deposit_cents, created_at)
           VALUES ('default', 'Optimizer Agent', 'o@test.com', 'Cedar', 'confirmed',
                   ?, ?, ?, ?, 50000, 0, '2026-02-03T09:00:00')""",
        (premium["id"], f"[{drone['id']}]", premium_listing_id, agent_id),
    )

    standard_invoice_id = invoices.create_invoice(
        standard_listing_id,
        title="Maple balance",
        amount_cents=20000,
        client_id=agent_id,
        line_items=[{"label": standard["name"], "qty": 1, "unit_cents": 20000}],
    )
    premium_invoice_id = invoices.create_invoice(
        premium_listing_id,
        title="Cedar balance",
        amount_cents=40000,
        client_id=agent_id,
        line_items=[
            {"label": premium["name"], "qty": 1, "unit_cents": 30000},
            {"label": drone["name"], "qty": 1, "unit_cents": 10000},
        ],
    )
    premium_open_id = invoices.create_invoice(
        premium_listing_id,
        title="Cedar add-on open",
        amount_cents=10000,
        client_id=agent_id,
        line_items=[{"label": drone["name"], "qty": 1, "unit_cents": 10000}],
    )
    manual_invoice_id = invoices.create_invoice(
        manual_listing_id,
        title="Manual standard",
        amount_cents=15000,
        client_id=agent_id,
        line_items=[{"label": standard["name"], "qty": 1, "unit_cents": 15000}],
    )

    for invoice_id in (standard_invoice_id, premium_invoice_id, manual_invoice_id):
        invoices.mark_paid(invoice_id)
    invoices.mark_sent(premium_open_id)

    if other_studio:
        db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other Studio', 'other')")
        other_listing_id = db.run(
            """INSERT INTO listings (studio_id, title, property_type, status, created_at)
               VALUES ('other', 'Other Optimizer Listing', 'commercial', 'delivered', '2026-04-01T09:00:00')""",
        )
        db.run(
            """INSERT INTO invoices (studio_id, listing_id, slug, title, amount_cents, status, paid_at)
               VALUES ('other', ?, 'other-opt-paid', 'Other paid', 99000, 'paid', '2026-04-02T09:00:00')""",
            (other_listing_id,),
        )


def _seed_acquisition_report(*, other_studio: bool = False):
    broker_id = db.run(
        """INSERT INTO clients (studio_id, name, client_type, portal_token)
           VALUES ('default', 'Good Realty Office', 'brokerage', 'good-office')""",
    )
    referrer_id = db.run(
        """INSERT INTO clients
           (studio_id, parent_id, name, client_type, company, email, portal_token)
           VALUES ('default', ?, 'Referrer Agent', 'agent', 'Good Realty',
                   'referrer@example.com', 'ref-agent')""",
        (broker_id,),
    )
    no_code_id = db.run(
        """INSERT INTO clients (studio_id, name, client_type, company, email, portal_token)
           VALUES ('default', 'No Code Agent', 'agent', 'Good Realty',
                   'nocode@example.com', 'nocode-agent')""",
    )
    zero_use_id = db.run(
        """INSERT INTO clients (studio_id, name, client_type, company, email, portal_token)
           VALUES ('default', 'Zero Use Agent', 'agent', 'Good Realty',
                   'zerouse@example.com', 'zerouse-agent')""",
    )
    referred_id = db.run(
        """INSERT INTO clients (studio_id, name, client_type, company, email, portal_token)
           VALUES ('default', 'Referred Agent', 'agent', 'New Realty',
                   'referred@example.com', 'referred-agent')""",
    )

    referrals.create_code(code="REF25", credit_cents=2500, referrer_client_id=referrer_id)
    referrals.create_code(code="ZERO25", credit_cents=2500, referrer_client_id=zero_use_id)

    ref_listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, created_at)
           VALUES ('default', ?, 'Referrer Listing', 'delivered', '2026-01-01T09:00:00')""",
        (referrer_id,),
    )
    no_code_first_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, created_at)
           VALUES ('default', ?, 'No Code First', 'delivered', '2026-01-02T09:00:00')""",
        (no_code_id,),
    )
    no_code_second_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, created_at)
           VALUES ('default', ?, 'No Code Second', 'delivered', '2026-02-02T09:00:00')""",
        (no_code_id,),
    )
    zero_use_listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, created_at)
           VALUES ('default', ?, 'Zero Use Listing', 'delivered', '2026-03-02T09:00:00')""",
        (zero_use_id,),
    )
    referred_listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, created_at)
           VALUES ('default', ?, 'Referred Listing', 'delivered', '2026-04-02T09:00:00')""",
        (referred_id,),
    )

    for listing_id, client_id, amount in (
        (ref_listing_id, referrer_id, 45000),
        (no_code_first_id, no_code_id, 20000),
        (no_code_second_id, no_code_id, 25000),
        (zero_use_listing_id, zero_use_id, 50000),
        (referred_listing_id, referred_id, 30000),
    ):
        invoice_id = invoices.create_invoice(
            listing_id,
            title=f"Invoice {listing_id}",
            amount_cents=amount,
            client_id=client_id,
        )
        invoices.mark_paid(invoice_id)
    open_id = invoices.create_invoice(
        referred_listing_id,
        title="Referred open",
        amount_cents=5000,
        client_id=referred_id,
    )
    invoices.mark_sent(open_id)
    db.run(
        """INSERT INTO inquiries
           (studio_id, name, email, property_address, status, promo_code,
            listing_id, client_id, total_cents, deposit_cents, created_at)
           VALUES ('default', 'Referred Agent', 'r@test.com', 'Referral House',
                   'confirmed', 'REF25', ?, ?, 35000, 0, '2026-04-01T09:00:00')""",
        (referred_listing_id, referred_id),
    )
    db.run(
        "UPDATE referral_codes SET uses=1 WHERE studio_id='default' AND code='REF25'",
    )

    if other_studio:
        db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other Studio', 'other')")
        other_client_id = db.run(
            """INSERT INTO clients (studio_id, name, client_type, portal_token)
               VALUES ('other', 'Other Agent', 'agent', 'other-agent-acq')""",
        )
        other_listing_id = db.run(
            """INSERT INTO listings (studio_id, client_id, title, status, created_at)
               VALUES ('other', ?, 'Other Referral Listing', 'delivered', '2026-05-01T09:00:00')""",
            (other_client_id,),
        )
        db.run(
            """INSERT INTO referral_codes
               (studio_id, code, credit_cents, referrer_client_id, uses)
               VALUES ('other', 'OTHER25', 2500, ?, 1)""",
            (other_client_id,),
        )
        db.run(
            """INSERT INTO inquiries
               (studio_id, name, email, property_address, status, promo_code,
                listing_id, client_id, total_cents, deposit_cents, created_at)
               VALUES ('other', 'Other Agent', 'o@test.com', 'Other House',
                       'confirmed', 'OTHER25', ?, ?, 99000, 0, '2026-05-01T09:00:00')""",
            (other_listing_id, other_client_id),
        )
        db.run(
            """INSERT INTO invoices (studio_id, listing_id, client_id, slug, title, amount_cents, status, paid_at)
               VALUES ('other', ?, ?, 'other-acq-paid', 'Other paid', 99000, 'paid', '2026-05-02T09:00:00')""",
            (other_listing_id, other_client_id),
        )
    return {
        "referrer_id": referrer_id,
        "no_code_id": no_code_id,
        "zero_use_id": zero_use_id,
    }


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
    assert rows[0]["n_active_agents"] == 2
    assert rows[0]["n_repeat_agents"] == 0
    assert rows[0]["agent_penetration_pct"] == 100
    assert rows[0]["n_listings"] == 2
    assert rows[0]["n_referral_codes"] == 1
    assert rows[0]["n_referral_uses"] == 1
    assert rows[0]["n_attributed_bookings"] == 1
    assert rows[0]["growth_stage"] == "Anchor office"
    assert (
        rows[0]["growth_next_action"]
        == "Ask the broker for two warm agent introductions this week."
    )
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
    assert summary["n_active_agents"] == 2
    assert summary["n_anchor_offices"] == 1
    assert summary["n_referral_codes"] == 1
    assert summary["n_attributed_bookings"] == 1
    assert summary["agent_penetration_pct"] == 100
    assert summary["paid_display"] == "$550"
    assert summary["open_display"] == "$250"

    body = brokerage_reports.brokerage_accounts_csv()
    assert "brokerage,company,agents,active_agents,repeat_agents,agent_penetration_pct" in body
    assert "Big Broker,,2,2,0,100,2,55000,25000,2,1,1,1,1,Anchor office" in body
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
        assert "Brokerage growth map" in r.text
        assert "Anchor office" in r.text
        assert "2/2 active" in r.text
        assert "1 codes" in r.text
        assert "Agent Kay" in r.text
        assert "Cedar Portfolio" in r.text

        export = await client.get("/admin/brokerages.csv", headers={"cookie": cookie})
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/csv")
        assert "eos-brokerages.csv" in export.headers["content-disposition"]
        assert "Big Broker,,2,2,0,100,2,55000,25000" in export.text


def test_revenue_optimizer_reports_packages_property_types_and_upsells(app_env):
    _seed_revenue_optimizer(other_studio=True)

    data = revenue_optimizer.dashboard()
    assert data["summary"]["paid_cents"] == 75000
    assert data["summary"]["open_cents"] == 10000
    assert data["summary"]["n_opportunities"] == 2
    assert data["summary"]["addon_attach_rate"] == 33

    packages = {row["name"]: row for row in data["packages"]}
    assert packages["Premium Listing"]["paid_cents"] == 40000
    assert packages["Premium Listing"]["open_cents"] == 10000
    assert packages["Premium Listing"]["addon_attach_rate"] == 100
    assert packages["Premium Listing"]["missed_upsell_count"] == 0
    assert packages["Standard Listing"]["paid_cents"] == 35000
    assert packages["Standard Listing"]["n_listings"] == 2
    assert packages["Standard Listing"]["missed_upsell_count"] == 2

    property_types = {row["name"]: row for row in data["property_types"]}
    assert property_types["Commercial"]["paid_cents"] == 40000
    assert property_types["Commercial"]["addon_attach_rate"] == 100
    assert property_types["Residential"]["paid_cents"] == 35000
    assert property_types["Residential"]["missed_upsell_count"] == 2

    opportunities = [row["title"] for row in data["opportunities"]]
    assert opportunities == ["Maple Standard", "Manual Standard"]
    assert "Other Optimizer Listing" not in opportunities

    body = revenue_optimizer.optimizer_csv()
    assert "Package performance" in body
    assert "Premium Listing,1,1,40000,10000" in body
    assert "Standard Listing,2,2,35000,0" in body
    assert "Other Optimizer Listing" not in body


@pytest.mark.asyncio
async def test_revenue_optimizer_dashboard_and_csv_routes(app_env):
    _seed_revenue_optimizer()

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]
        r = await client.get("/admin/reports/revenue-optimizer", headers={"cookie": cookie})
        assert r.status_code == 200
        assert "Revenue optimizer" in r.text
        assert "Package performance" in r.text
        assert "Premium Listing" in r.text
        assert "Standard Listing" in r.text
        assert "Upsell opportunities" in r.text
        assert "Maple Standard" in r.text
        assert "Aerial / drone photos" in r.text

        export = await client.get(
            "/admin/reports/revenue-optimizer.csv", headers={"cookie": cookie}
        )
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/csv")
        assert "eos-revenue-optimizer.csv" in export.headers["content-disposition"]
        assert "Package performance" in export.text
        assert "Maple Standard,Optimizer Agent,Standard Listing" in export.text


def test_acquisition_report_tracks_referrals_and_intro_asks(app_env):
    ids = _seed_acquisition_report(other_studio=True)

    data = acquisition.dashboard()
    assert data["summary"]["n_active_codes"] == 2
    assert data["summary"]["n_referred_listings"] == 1
    assert data["summary"]["referred_paid_cents"] == 30000
    assert data["summary"]["referred_open_cents"] == 5000

    referral_rows = {row["code"]: row for row in data["referrals"]}
    assert referral_rows["REF25"]["referrer_name"] == "Referrer Agent"
    assert referral_rows["REF25"]["uses"] == 1
    assert referral_rows["REF25"]["n_inquiries"] == 1
    assert referral_rows["REF25"]["referred_paid_display"] == "$300"
    assert referral_rows["ZERO25"]["uses"] == 0
    assert "OTHER25" not in referral_rows

    attribution_rows = {row["promo_code"]: row for row in data["attribution"]}
    assert attribution_rows["REF25"]["source_type"] == "referral"
    assert attribution_rows["REF25"]["source_url"].endswith("/book?ref=REF25")
    assert attribution_rows["REF25"]["referrer_name"] == "Referrer Agent"
    assert attribution_rows["REF25"]["brokerage_name"] == "Good Realty Office"
    assert attribution_rows["REF25"]["paid_display"] == "$300"
    assert attribution_rows["REF25"]["open_display"] == "$50"
    assert data["attribution_summary"]["n_attributed_bookings"] == 1
    assert data["attribution_summary"]["n_referral_bookings"] == 1
    assert data["attribution_summary"]["n_brokerages"] == 1
    assert data["attribution_summary"]["paid_cents"] == 30000
    assert data["attribution_summary"]["open_cents"] == 5000

    agent_rows = {row["name"]: row for row in data["agent_referrals"]}
    assert agent_rows["Referrer Agent"]["code_list"] == "REF25"
    assert agent_rows["Referrer Agent"]["email"] == "referrer@example.com"
    assert agent_rows["Referrer Agent"]["uses"] == 1
    assert agent_rows["Referrer Agent"]["n_listings"] == 1
    assert agent_rows["Referrer Agent"]["referred_paid_display"] == "$300"
    assert agent_rows["Referrer Agent"]["intro_status"] == "ready"
    assert agent_rows["Zero Use Agent"]["code_list"] == "ZERO25"
    assert "Other Agent" not in agent_rows

    ask_rows = {row["name"]: row for row in data["intro_asks"]}
    assert ask_rows["No Code Agent"]["id"] == ids["no_code_id"]
    assert ask_rows["No Code Agent"]["action"] == "Create referral code"
    assert ask_rows["No Code Agent"]["suggested_code"] == "NOCODEAG25"
    assert ask_rows["Zero Use Agent"]["id"] == ids["zero_use_id"]
    assert ask_rows["Zero Use Agent"]["action"] == "Ask for an introduction"
    assert "Referrer Agent" not in ask_rows
    assert "Other Agent" not in ask_rows

    body = acquisition.acquisition_csv()
    assert "Referral codes" in body
    assert "REF25,Referrer Agent,Good Realty,referrer@example.com,1" in body
    assert "Attribution" in body
    assert "/book?ref=REF25" in body
    assert "REF25,Referrer Agent,Good Realty,Good Realty Office,Referred Agent" in body
    assert "Agent referral summary" in body
    assert (
        "Referrer Agent,Good Realty,referrer@example.com,Good Realty Office,REF25,1,1,1,1,30000,5000"
        in body
    )
    assert "No Code Agent,Good Realty,,2,45000" in body
    assert "OTHER25" not in body


def test_acquisition_intro_email_sends_and_cooldown_logs(app_env, monkeypatch):
    ids = _seed_acquisition_report()
    sent: list[tuple[str, str, str]] = []

    monkeypatch.setattr(acquisition.mailer, "configured", lambda: True)
    monkeypatch.setattr(
        acquisition.mailer,
        "send_for_studio",
        lambda to, subject, body: sent.append((to, subject, body)),
    )

    result = acquisition.send_intro_email(ids["no_code_id"])
    second = acquisition.send_intro_email(ids["no_code_id"])

    assert result["status"] == "sent"
    assert second["status"] == "cooldown"
    assert len(sent) == 1
    assert sent[0][0] == "nocode@example.com"
    assert "NOCODEAG25" in sent[0][2]
    assert "/book" in sent[0][2]
    email_log = db.one(
        """SELECT doc_kind, doc_id, to_email
           FROM emails_log
           WHERE studio_id=? AND doc_kind='acquisition_intro'""",
        ("default",),
    )
    assert dict(email_log) == {
        "doc_kind": "acquisition_intro",
        "doc_id": ids["no_code_id"],
        "to_email": "nocode@example.com",
    }
    audit = db.one(
        "SELECT action, detail FROM audit_log WHERE studio_id=? AND action=?",
        ("default", acquisition.ACTION_SENT),
    )
    assert audit["detail"].startswith(f"client_id={ids['no_code_id']};")

    ask_rows = {row["name"]: row for row in acquisition.dashboard()["intro_asks"]}
    assert ask_rows["No Code Agent"]["cooldown_active"] is True
    assert ask_rows["No Code Agent"]["can_email_intro"] is False


def test_acquisition_intro_email_drafts_and_requires_current_studio_agent(app_env, monkeypatch):
    ids = _seed_acquisition_report()
    monkeypatch.setattr(acquisition.mailer, "configured", lambda: False)

    result = acquisition.send_intro_email(ids["zero_use_id"])

    assert result["status"] == "draft"
    assert result["draft"]["to"] == "zerouse@example.com"
    assert result["draft"]["active_referral_code"] == "ZERO25"
    assert "ZERO25" in result["draft"]["body"]
    assert db.one("SELECT 1 FROM emails_log WHERE doc_kind='acquisition_intro'") is None
    audit = db.one(
        "SELECT action, detail FROM audit_log WHERE studio_id=? AND action=?",
        ("default", acquisition.ACTION_DRAFT),
    )
    assert audit["detail"].startswith(f"client_id={ids['zero_use_id']};")

    db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other Studio', 'other')")
    other_id = db.run(
        """INSERT INTO clients (studio_id, name, client_type, email)
           VALUES ('other', 'Other Agent', 'agent', 'other@example.com')""",
    )
    with pytest.raises(HTTPException):
        acquisition.build_intro_email(other_id)


def test_acquisition_queue_filters_and_bulk_send_reuse_cooldown(app_env, monkeypatch):
    _seed_acquisition_report()
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(acquisition.mailer, "configured", lambda: True)
    monkeypatch.setattr(
        acquisition.mailer,
        "send_for_studio",
        lambda to, subject, body: sent.append((to, subject, body)),
    )

    data = acquisition.dashboard()
    filter_counts = {row["key"]: row["count"] for row in data["queue_filters"]}
    assert filter_counts == {
        "all": 2,
        "needs_code": 1,
        "needs_intro": 1,
        "ready": 2,
        "cooldown": 0,
    }
    assert [
        row["name"] for row in acquisition.dashboard(queue_filter="needs_code")["intro_asks"]
    ] == ["No Code Agent"]
    assert [
        row["name"] for row in acquisition.dashboard(queue_filter="needs_intro")["intro_asks"]
    ] == ["Zero Use Agent"]

    result = acquisition.bulk_send_intro_emails(queue_filter="ready")
    second = acquisition.bulk_send_intro_emails(queue_filter="all")

    assert result["sent"] == 2
    assert result["draft"] == 0
    assert result["skipped"] == 0
    assert len(sent) == 2
    assert second["cooldown"] == 2
    assert second["sent"] == 0
    assert acquisition.dashboard(queue_filter="ready")["intro_asks"] == []
    assert {
        row["name"] for row in acquisition.dashboard(queue_filter="cooldown")["intro_asks"]
    } == {
        "No Code Agent",
        "Zero Use Agent",
    }
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM emails_log WHERE studio_id=? AND doc_kind='acquisition_intro'",
            ("default",),
        )["n"]
        == 2
    )


@pytest.mark.asyncio
async def test_acquisition_dashboard_and_csv_routes(app_env):
    _seed_acquisition_report()

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]
        r = await client.get("/admin/reports/acquisition", headers={"cookie": cookie})
        assert r.status_code == 200
        assert "Agent acquisition" in r.text
        assert "Intro ask queue" in r.text
        assert "No Code Agent" in r.text
        assert "Zero Use Agent" in r.text
        assert "REF25" in r.text
        assert "$300" in r.text
        assert "Attributed bookings" in r.text
        assert "Good Realty Office" in r.text
        assert "Draft intro ask" in r.text
        assert "Needs code (1)" in r.text
        assert "Needs intro (1)" in r.text
        assert "Ready (2)" in r.text

        export = await client.get("/admin/reports/acquisition.csv", headers={"cookie": cookie})
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/csv")
        assert "eos-acquisition.csv" in export.headers["content-disposition"]
        assert "Referral codes" in export.text
        assert "REF25,Referrer Agent,Good Realty,referrer@example.com,1" in export.text
        assert "Agent referral summary" in export.text


@pytest.mark.asyncio
async def test_acquisition_bulk_route_drafts_visible_queue(app_env, monkeypatch):
    _seed_acquisition_report()
    monkeypatch.setattr(acquisition.mailer, "configured", lambda: False)

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]
        bulk = await client.post(
            "/admin/reports/acquisition/bulk-send",
            data={"queue_filter": "ready"},
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        assert bulk.status_code == 303
        assert "acquisition=bulk" in bulk.headers["location"]
        assert "draft=2" in bulk.headers["location"]

        page = await client.get(bulk.headers["location"], headers={"cookie": cookie})

    assert page.status_code == 200
    assert "Bulk intro ask complete" in page.text
    assert "2 drafted" in page.text
    assert db.one("SELECT 1 FROM emails_log WHERE doc_kind='acquisition_intro'") is None
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM audit_log WHERE studio_id=? AND action=?",
            ("default", acquisition.ACTION_DRAFT),
        )["n"]
        == 2
    )


@pytest.mark.asyncio
async def test_studio_settings_show_agent_referral_performance(app_env):
    _seed_acquisition_report()

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]
        r = await client.get("/admin/studio", headers={"cookie": cookie})

    assert r.status_code == 200
    assert "Agent referral performance" in r.text
    assert "Referrer Agent" in r.text
    assert "ZERO25" in r.text
    assert "$300" in r.text


@pytest.mark.asyncio
async def test_acquisition_intro_route_drafts_and_shows_mailto(app_env, monkeypatch):
    ids = _seed_acquisition_report()
    monkeypatch.setattr(acquisition.mailer, "configured", lambda: False)

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]
        sent = await client.post(
            f"/admin/reports/acquisition/{ids['zero_use_id']}/send",
            data={"redirect": "/admin/reports/acquisition"},
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        assert sent.status_code == 303
        assert "acquisition=draft" in sent.headers["location"]

        page = await client.get(sent.headers["location"], headers={"cookie": cookie})

    assert page.status_code == 200
    assert "Referral introduction email draft" in page.text
    assert "zerouse@example.com" in page.text
    assert "ZERO25" in page.text


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
