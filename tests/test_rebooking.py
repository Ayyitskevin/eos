import datetime as dt

import eos.churn as churn
import eos.clients as clients
import eos.db as db
import eos.invoices as invoices
import eos.listings as listings
import eos.rebooking as rebooking
import eos.security as security
import eos.tenant as tenant
import pytest
from fastapi import HTTPException
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


def _age_rebooking_sent(client_id: int, days: int) -> None:
    db.run(
        "UPDATE audit_log SET created_at=datetime('now', ?) WHERE action=? AND detail LIKE ?",
        (f"-{days} days", rebooking.ACTION_SENT, f"client_id={client_id};%"),
    )
    db.run(
        "UPDATE emails_log SET sent_at=datetime('now', ?) WHERE doc_kind='rebooking' AND doc_id=? AND studio_id=?",
        (f"-{days} days", client_id, "default"),
    )


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


def test_rebooking_email_sends_and_cooldown_logs(app_env, monkeypatch):
    tenant.set_studio("default")
    agent_id = clients.create_client("Email Agent", email="email@example.com", client_type="agent")
    _delivered_listing(agent_id, "Email listing")
    sent: list[tuple[str, str, str]] = []

    monkeypatch.setattr(rebooking.mailer, "configured", lambda: True)
    monkeypatch.setattr(
        rebooking.mailer,
        "send_for_studio",
        lambda to, subject, body: sent.append((to, subject, body)),
    )

    result = rebooking.send_email(agent_id)
    second = rebooking.send_email(agent_id)

    assert result["status"] == "sent"
    assert second["status"] == "cooldown"
    assert len(sent) == 1
    assert sent[0][0] == "email@example.com"
    assert "/book" in sent[0][2]
    email_log = db.one(
        "SELECT doc_kind, doc_id, to_email FROM emails_log WHERE studio_id=? AND doc_kind='rebooking'",
        ("default",),
    )
    assert dict(email_log) == {
        "doc_kind": "rebooking",
        "doc_id": agent_id,
        "to_email": "email@example.com",
    }
    audit = db.one(
        "SELECT action, detail FROM audit_log WHERE studio_id=? AND action=?",
        ("default", rebooking.ACTION_SENT),
    )
    assert audit["detail"].startswith(f"client_id={agent_id};")


def test_rebooking_email_drafts_when_mailer_is_not_configured(app_env, monkeypatch):
    tenant.set_studio("default")
    agent_id = clients.create_client("Draft Agent", email="draft@example.com", client_type="agent")
    _delivered_listing(agent_id, "Draft listing")
    monkeypatch.setattr(rebooking.mailer, "configured", lambda: False)

    result = rebooking.send_email(agent_id)

    assert result["status"] == "draft"
    assert result["draft"]["to"] == "draft@example.com"
    assert "Ready for your next listing?" == result["draft"]["subject"]
    assert db.one("SELECT 1 FROM emails_log WHERE doc_kind='rebooking'") is None
    audit = db.one(
        "SELECT action, detail FROM audit_log WHERE studio_id=? AND action=?",
        ("default", rebooking.ACTION_DRAFT),
    )
    assert "client_id=" in audit["detail"]


def test_rebooking_email_requires_current_studio_agent(app_env):
    tenant.set_studio("default")
    db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other', 'other')")
    other_id = db.run(
        """INSERT INTO clients (studio_id, client_type, name, email)
           VALUES ('other', 'agent', 'Other Agent', 'other@example.com')"""
    )

    with pytest.raises(HTTPException):
        rebooking.build_email(other_id)


def test_rebooking_performance_snapshot_counts_conversions(app_env, monkeypatch):
    tenant.set_studio("default")
    ready_id = clients.create_client("Ready Agent", email="ready@example.com", client_type="agent")
    converted_id = clients.create_client(
        "Converted Agent", email="converted@example.com", client_type="agent"
    )
    _delivered_listing(ready_id, "Ready old listing")
    _delivered_listing(converted_id, "Converted old listing")

    monkeypatch.setattr(rebooking.mailer, "configured", lambda: True)
    monkeypatch.setattr(rebooking.mailer, "send_for_studio", lambda to, subject, body: None)
    assert rebooking.send_email(converted_id)["status"] == "sent"
    new_listing = listings.create_listing(
        "Converted new listing", client_id=converted_id, seed_defaults=False
    )
    db.run(
        "UPDATE listings SET created_at=datetime('now', '+1 minute') WHERE id=? AND studio_id=?",
        (new_listing, "default"),
    )

    snapshot = rebooking.performance_snapshot()

    assert snapshot["ready_count"] == 1
    assert snapshot["sent_recent"] == 1
    assert snapshot["converted_recent"] == 1
    assert snapshot["conversion_rate_pct"] == 100
    assert snapshot["converted_listings"][0]["listing_title"] == "Converted new listing"
    assert "ready for outreach" in snapshot["next_action"]


def test_rebooking_client_history_surfaces_conversion(app_env, monkeypatch):
    tenant.set_studio("default")
    agent_id = clients.create_client(
        "History Agent", email="history@example.com", client_type="agent"
    )
    _delivered_listing(agent_id, "History old listing")
    monkeypatch.setattr(rebooking.mailer, "configured", lambda: True)
    monkeypatch.setattr(rebooking.mailer, "send_for_studio", lambda to, subject, body: None)
    assert rebooking.send_email(agent_id)["status"] == "sent"
    listing_id = listings.create_listing(
        "History new listing", client_id=agent_id, seed_defaults=False
    )

    history = rebooking.client_history(agent_id)

    assert history["latest_sent_at"]
    assert history["converted_listing"]["id"] == listing_id
    assert history["converted_listing"]["title"] == "History new listing"


def test_rebooking_follow_up_queue_lists_unconverted_old_sends(app_env, monkeypatch):
    tenant.set_studio("default")
    due_id = clients.create_client("Due Agent", email="due@example.com", client_type="agent")
    converted_id = clients.create_client(
        "Converted Followup", email="converted-followup@example.com", client_type="agent"
    )
    fresh_id = clients.create_client("Fresh Agent", email="fresh@example.com", client_type="agent")
    for client_id, title in (
        (due_id, "Due old listing"),
        (converted_id, "Converted followup old listing"),
        (fresh_id, "Fresh old listing"),
    ):
        _delivered_listing(client_id, title)

    monkeypatch.setattr(rebooking.mailer, "configured", lambda: True)
    monkeypatch.setattr(rebooking.mailer, "send_for_studio", lambda to, subject, body: None)
    for client_id in (due_id, converted_id, fresh_id):
        assert rebooking.send_email(client_id)["status"] == "sent"
    _age_rebooking_sent(due_id, 8)
    _age_rebooking_sent(converted_id, 8)
    _age_rebooking_sent(fresh_id, 3)
    listings.create_listing("Converted followup new", client_id=converted_id, seed_defaults=False)

    db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other', 'other')")
    other_id = db.run(
        """INSERT INTO clients (studio_id, client_type, name, email)
           VALUES ('other', 'agent', 'Other Followup', 'other-followup@example.com')"""
    )
    db.run(
        """INSERT INTO audit_log (studio_id, actor, action, detail, created_at)
           VALUES ('other', 'admin', ?, ?, datetime('now', '-12 days'))""",
        (rebooking.ACTION_SENT, f"client_id={other_id}; email=other-followup@example.com;"),
    )

    queue = rebooking.follow_up_queue()

    assert [item["id"] for item in queue] == [due_id]
    assert queue[0]["days_waiting"] >= 7
    assert queue[0]["can_send_again"] is False
    assert queue[0]["next_action"] == "Manual check-in"


def test_rebooking_follow_up_queue_allows_second_touch_after_cooldown(app_env, monkeypatch):
    tenant.set_studio("default")
    agent_id = clients.create_client("Second Touch Agent", email="second@example.com")
    _delivered_listing(agent_id, "Second old listing")
    monkeypatch.setattr(rebooking.mailer, "configured", lambda: True)
    monkeypatch.setattr(rebooking.mailer, "send_for_studio", lambda to, subject, body: None)
    rebooking.send_email(agent_id)
    _age_rebooking_sent(agent_id, 16)

    queue = rebooking.follow_up_queue()

    assert queue[0]["id"] == agent_id
    assert queue[0]["can_send_again"] is True
    assert queue[0]["next_action"] == "Send second touch"


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
        assert "Ready to nudge" in dashboard.text
        assert "Repeat Agent" in dashboard.text
        assert f"/admin/listings/new?client_id={agent_id}" in dashboard.text

        form = await client.get(
            f"/admin/listings/new?client_id={agent_id}", headers={"cookie": cookie}
        )
        assert form.status_code == 200
        assert f'<option value="{agent_id}" selected>' in form.text


@pytest.mark.asyncio
async def test_dashboard_shows_rebooking_follow_up_queue(app_env, monkeypatch):
    tenant.set_studio("default")
    agent_id = clients.create_client("Followup Agent", email="followup@example.com")
    _delivered_listing(agent_id, "Followup old listing")
    monkeypatch.setattr(rebooking.mailer, "configured", lambda: True)
    monkeypatch.setattr(rebooking.mailer, "send_for_studio", lambda to, subject, body: None)
    rebooking.send_email(agent_id)
    _age_rebooking_sent(agent_id, 8)

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]

        dashboard = await client.get("/admin", headers={"cookie": cookie})

    assert dashboard.status_code == 200
    assert "Follow-ups" in dashboard.text
    assert "Manual check-in" in dashboard.text
    assert "Followup Agent" in dashboard.text


@pytest.mark.asyncio
async def test_rebooking_send_route_redirects_with_notice(app_env, monkeypatch):
    tenant.set_studio("default")
    agent_id = clients.create_client("Route Agent", email="route@example.com", client_type="agent")
    _delivered_listing(agent_id, "Route listing")
    monkeypatch.setattr(rebooking.mailer, "configured", lambda: True)
    monkeypatch.setattr(rebooking.mailer, "send_for_studio", lambda to, subject, body: None)

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        csrf = client.cookies.get(security.CSRF_COOKIE)

        sent = await client.post(
            f"/admin/rebooking/{agent_id}/send",
            data={"redirect": "/admin", security.CSRF_FORM: csrf},
            headers={"sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )

    assert sent.status_code == 303
    assert sent.headers["location"] == f"/admin?rebooking=sent&client_id={agent_id}"


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


@pytest.mark.asyncio
async def test_client_detail_shows_rebooking_draft_when_email_unconfigured(app_env, monkeypatch):
    tenant.set_studio("default")
    agent_id = clients.create_client("Draft Detail Agent", email="detail@example.com")
    _delivered_listing(agent_id, "Draft detail listing")
    monkeypatch.setattr(rebooking.mailer, "configured", lambda: False)

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]

        page = await client.get(
            f"/admin/clients/{agent_id}?rebooking=draft", headers={"cookie": cookie}
        )

    assert page.status_code == 200
    assert "Rebooking email draft" in page.text
    assert "detail@example.com" in page.text


@pytest.mark.asyncio
async def test_client_detail_shows_rebooking_conversion(app_env, monkeypatch):
    tenant.set_studio("default")
    agent_id = clients.create_client("Converted Detail Agent", email="converted-detail@example.com")
    _delivered_listing(agent_id, "Converted detail old")
    monkeypatch.setattr(rebooking.mailer, "configured", lambda: True)
    monkeypatch.setattr(rebooking.mailer, "send_for_studio", lambda to, subject, body: None)
    rebooking.send_email(agent_id)
    listings.create_listing("Converted detail new", client_id=agent_id, seed_defaults=False)

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]

        page = await client.get(f"/admin/clients/{agent_id}", headers={"cookie": cookie})

    assert page.status_code == 200
    assert "Rebooking outreach" in page.text
    assert "Converted detail new" in page.text


@pytest.mark.asyncio
async def test_client_detail_shows_rebooking_follow_up_due(app_env, monkeypatch):
    tenant.set_studio("default")
    agent_id = clients.create_client("Due Detail Agent", email="due-detail@example.com")
    _delivered_listing(agent_id, "Due detail old")
    monkeypatch.setattr(rebooking.mailer, "configured", lambda: True)
    monkeypatch.setattr(rebooking.mailer, "send_for_studio", lambda to, subject, body: None)
    rebooking.send_email(agent_id)
    _age_rebooking_sent(agent_id, 8)

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]

        page = await client.get(f"/admin/clients/{agent_id}", headers={"cookie": cookie})

    assert page.status_code == 200
    assert "Follow-up due" in page.text
    assert "days ago with no new listing yet" in page.text
