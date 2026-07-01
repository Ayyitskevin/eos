import datetime as dt

import eos.churn as churn
import eos.clients as clients
import eos.db as db
import eos.invoices as invoices
import eos.listings as listings
import eos.tenant as tenant
import pytest
from httpx import ASGITransport, AsyncClient


def _old_date(days: int = 120) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def _delivered_listing(client_id: int, title: str, *, days: int = 120) -> int:
    listing_id = listings.create_listing(title, client_id=client_id, seed_defaults=False)
    db.run(
        "UPDATE listings SET status='delivered', created_at=? WHERE id=? AND studio_id=?",
        (_old_date(days), listing_id, "default"),
    )
    return listing_id


def _paid_invoice(listing_id: int, client_id: int, amount_cents: int) -> int:
    invoice_id = invoices.create_invoice(
        listing_id,
        title="Listing package",
        amount_cents=amount_cents,
        client_id=client_id,
        agent_client_id=client_id,
    )
    invoices.mark_paid(invoice_id)
    return invoice_id


def test_rebooking_opportunities_rank_value_and_scope_by_studio(app_env):
    tenant.set_studio("default")
    warm_id = clients.create_client("Warm Agent", email="warm@example.com", client_type="agent")
    high_id = clients.create_client("High Value", email="high@example.com", client_type="agent")

    _delivered_listing(warm_id, "Warm listing")
    high_listing = _delivered_listing(high_id, "High listing")
    _paid_invoice(high_listing, high_id, 75_000)

    db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other', 'other')")
    other_id = db.run(
        """INSERT INTO clients (studio_id, client_type, name, email)
           VALUES ('other', 'agent', 'Other Studio Agent', 'other@example.com')"""
    )
    other_listing = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, created_at)
           VALUES ('other', ?, 'Other listing', 'delivered', ?)""",
        (other_id, _old_date()),
    )
    db.run(
        """INSERT INTO invoices
           (studio_id, listing_id, client_id, agent_client_id, slug, title, amount_cents, status)
           VALUES ('other', ?, ?, ?, 'other-paid', 'Other paid', 200000, 'paid')""",
        (other_listing, other_id, other_id),
    )

    rows = churn.rebooking_opportunities(days=90, limit=5)

    assert [r["id"] for r in rows[:2]] == [high_id, warm_id]
    assert rows[0]["priority"] == "High value"
    assert rows[0]["paid_display"] == "$750"
    assert rows[0]["book_href"] == f"/admin/listings/new?client_id={high_id}"
    assert "Other Studio Agent" not in {r["name"] for r in rows}


def test_rebooking_opportunities_skip_agents_with_active_listings(app_env):
    tenant.set_studio("default")
    agent_id = clients.create_client("Busy Agent", email="busy@example.com", client_type="agent")
    listings.create_listing("Active listing", client_id=agent_id, seed_defaults=False)

    rows = churn.rebooking_opportunities(days=90, limit=5)

    assert agent_id not in {r["id"] for r in rows}


@pytest.mark.asyncio
async def test_dashboard_rebooking_cockpit_and_listing_preselect(app_env):
    tenant.set_studio("default")
    agent_id = clients.create_client(
        "Repeat Agent", email="repeat@example.com", client_type="agent"
    )
    _delivered_listing(agent_id, "Repeat listing")

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]

        dashboard = await client.get("/admin", headers={"cookie": cookie})
        assert dashboard.status_code == 200
        assert "Agent rebooking cockpit" in dashboard.text
        assert "Repeat Agent" in dashboard.text
        assert f"/admin/listings/new?client_id={agent_id}" in dashboard.text

        form = await client.get(
            f"/admin/listings/new?client_id={agent_id}", headers={"cookie": cookie}
        )
        assert form.status_code == 200
        assert f'<option value="{agent_id}" selected>' in form.text


@pytest.mark.asyncio
async def test_client_detail_shows_repeat_listing_cta(app_env):
    tenant.set_studio("default")
    agent_id = clients.create_client("Client Detail Agent", email="client@example.com")
    _delivered_listing(agent_id, "Client detail listing")

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]

        page = await client.get(f"/admin/clients/{agent_id}", headers={"cookie": cookie})

    assert page.status_code == 200
    assert "Repeat listing opportunity" in page.text
    assert f"/admin/listings/new?client_id={agent_id}" in page.text
