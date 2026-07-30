"""Phase 21 — gallery-view analytics + scheduled agent digest emails."""

import eos.analytics as analytics
import eos.clients as clients
import eos.db as db
import eos.mailer as mailer
import eos.portal as portal
import eos.rbac as rbac
import eos.security as security
import eos.studio as studio_mod
import eos.tenant as tenant
import eos.users as users
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request


def _seed_listing(
    studio: str = "default",
    title: str = "123 Main St",
    email: str = "agent@example.com",
) -> tuple[int, int, int]:
    tenant.set_studio(studio)
    client_id = db.run(
        "INSERT INTO clients (studio_id, client_type, name, email) VALUES (?,?,?,?)",
        (studio, "agent", "Agent One", email),
    )
    listing_id = db.run(
        """INSERT INTO listings
           (studio_id, client_id, title, status, address_line1, site_slug, site_published)
           VALUES (?,?,?,'delivered',?,?,1)""",
        (studio, client_id, title, title, f"site-{studio}-{listing_slug(title)}"),
    )
    gallery_id = db.run(
        """INSERT INTO galleries
           (studio_id, listing_id, slug, title, pin, delivery_token, published)
           VALUES (?,?,?,?,?,?,1)""",
        (
            studio,
            listing_id,
            f"gal-{studio}-{listing_slug(title)}",
            title,
            "1234",
            security.new_token(),
        ),
    )
    return client_id, listing_id, gallery_id


def listing_slug(title: str) -> str:
    return title.lower().replace(" ", "-")


def _view(
    studio: str,
    listing_id: int,
    gallery_id: int | None,
    *,
    event_type: str = "gallery_view",
    visitor: str = "visitor-1",
    referrer: str = "example.com",
    ago_days: int = 0,
) -> None:
    db.run(
        """INSERT INTO view_events
           (studio_id, event_type, listing_id, gallery_id, visitor_key, referrer_domain, created_at)
           VALUES (?,?,?,?,?,?, datetime('now', ?))""",
        (studio, event_type, listing_id, gallery_id, visitor, referrer, f"-{ago_days} days"),
    )


def _events(studio: str = "default") -> list:
    return db.all_("SELECT * FROM view_events WHERE studio_id=?", (studio,))


async def _unlock_gallery(client: AsyncClient, slug: str) -> None:
    r = await client.post(f"/g/{slug}/pin", data={"pin": "1234"}, follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.asyncio
async def test_gallery_view_records_privacy_preserving_event(app_env):
    _client_id, listing_id, gallery_id = _seed_listing()
    slug = f"gal-default-{listing_slug('123 Main St')}"
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _unlock_gallery(client, slug)
        r = await client.get(
            f"/g/{slug}",
            headers={"referer": "https://www.zillow.com/homes/123-main?utm_source=email"},
        )
        assert r.status_code == 200
    rows = _events()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "gallery_view"
    assert row["listing_id"] == listing_id
    assert row["gallery_id"] == gallery_id
    assert row["visitor_key"]
    assert row["visitor_key"] != "testclient"
    assert "." not in row["visitor_key"]  # no raw IPv4 at rest
    assert row["referrer_domain"] == "www.zillow.com"  # query string stripped


@pytest.mark.asyncio
async def test_microsite_view_records_event(app_env):
    _client_id, listing_id, _gallery_id = _seed_listing()
    slug = f"site-default-{listing_slug('123 Main St')}"
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get(f"/l/{slug}")
        assert r.status_code == 200
    rows = _events()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "microsite_view"
    assert rows[0]["listing_id"] == listing_id


@pytest.mark.asyncio
async def test_do_not_track_header_skips_tracking(app_env):
    _seed_listing()
    slug = f"site-default-{listing_slug('123 Main St')}"
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get(f"/l/{slug}", headers={"DNT": "1"})
        assert r.status_code == 200
    assert _events() == []


def test_analytics_dashboard_scopes_by_studio(app_env):
    _cid, listing_id, gallery_id = _seed_listing(title="123 Main St")
    _view("default", listing_id, gallery_id)
    _view("default", listing_id, gallery_id, visitor="visitor-2")
    _view("default", listing_id, gallery_id, event_type="microsite_view")

    db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other', 'other')")
    _ocid, other_listing, other_gallery = _seed_listing(
        studio="other", title="999 Other St", email="other@example.com"
    )
    _view("other", other_listing, other_gallery)

    tenant.set_studio("default")
    data = analytics.dashboard(30)
    assert data["summary"]["views"] == 3
    assert data["summary"]["unique_visitors"] == 2
    assert [row["address"] for row in data["listings"]] == ["123 Main St"]
    row = data["listings"][0]
    assert row["gallery_views"] == 2
    assert row["microsite_views"] == 1
    assert data["referrers"] == [{"domain": "example.com", "views": 3}]

    csv_body = analytics.analytics_csv(30)
    assert "123 Main St" in csv_body
    assert "999 Other St" not in csv_body

    tenant.set_studio("other")
    other = analytics.dashboard(30)
    assert other["summary"]["views"] == 1
    assert other["listings"][0]["listing_id"] == other_listing


def test_analytics_trend_compares_prior_period(app_env):
    _cid, listing_id, gallery_id = _seed_listing()
    _view("default", listing_id, gallery_id, ago_days=2)
    _view("default", listing_id, gallery_id, ago_days=10)
    tenant.set_studio("default")
    data = analytics.dashboard(7)
    row = data["listings"][0]
    assert row["views"] == 1
    assert row["prior_views"] == 1
    assert row["trend_pct"] == 0


async def _admin_client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    login = await client.post(
        "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
    )
    assert login.status_code == 303
    return client


@pytest.mark.asyncio
async def test_admin_analytics_report_and_csv(app_env):
    _cid, listing_id, gallery_id = _seed_listing()
    _view("default", listing_id, gallery_id)
    client = await _admin_client(app_env)
    try:
        for days in (7, 30, 90):
            r = await client.get(f"/admin/reports/analytics?days={days}")
            assert r.status_code == 200
            assert "123 Main St" in r.text
            assert "Listing analytics" in r.text
        csv_resp = await client.get("/admin/reports/analytics.csv?days=30")
        assert csv_resp.status_code == 200
        assert csv_resp.headers["content-type"].startswith("text/csv")
        assert "unique_visitors" in csv_resp.text
        assert "123 Main St" in csv_resp.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_analytics_report_requires_admin(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/admin/reports/analytics", follow_redirects=False)
        assert r.status_code == 303
        assert "/admin/login" in r.headers["location"]


def _role_request(user_id: int, method: str, path: str) -> Request:
    name, value = security.set_session_cookie(user_id)
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"cookie", f"{name}={value}".encode())],
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 12345),
        }
    )


def test_analytics_rbac_fail_closed(app_env):
    tenant.set_studio("default")
    scheduler_id = users.create_user("sched@example.com", "pass12345", role="scheduler")
    accountant_id = users.create_user("acct@example.com", "pass12345", role="accountant")
    for path in ("/admin/reports/analytics", "/admin/reports/analytics.csv"):
        with pytest.raises(HTTPException) as exc_info:
            rbac.check_route(_role_request(scheduler_id, "GET", path))
        assert exc_info.value.status_code == 403
        # Accountants hold reports.read and may view analytics.
        rbac.check_route(_role_request(accountant_id, "GET", path))


def test_digest_intent_persisted_before_send_and_idempotent(app_env, monkeypatch):
    client_id, listing_id, gallery_id = _seed_listing()
    _view("default", listing_id, gallery_id)
    _view("default", listing_id, gallery_id, visitor="visitor-2")
    _view("default", listing_id, gallery_id, event_type="microsite_view")

    tenant.set_studio("default")
    # Mailer is not configured: the intent is still persisted first.
    assert analytics.enqueue_weekly_digests() == 1
    intent = db.one("SELECT * FROM analytics_digest_intents WHERE studio_id='default'")
    assert intent["status"] == "pending"
    assert intent["client_id"] == client_id
    assert intent["to_email"] == "agent@example.com"
    assert "views this week" in intent["subject"]
    assert analytics.process_pending() == 0

    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(
        mailer, "send_for_studio", lambda to, subject, body: sent.append((to, subject, body))
    )
    assert analytics.process_pending() == 1
    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == "agent@example.com"
    assert subject == "Your listings got 3 views this week"
    assert "123 Main St — 3 views · 2 unique visitors" in body
    assert f"/l/site-default-{listing_slug('123 Main St')}" in body

    intent = db.one("SELECT * FROM analytics_digest_intents WHERE studio_id='default'")
    assert intent["status"] == "sent"
    assert db.one(
        "SELECT 1 AS x FROM emails_log WHERE studio_id='default' AND doc_kind='analytics_digest'"
    )

    # Re-running the sweep in the same ISO week neither re-enqueues nor re-sends.
    assert analytics.enqueue_weekly_digests() == 0
    assert analytics.process_pending() == 0
    assert len(sent) == 1
    assert db.one("SELECT COUNT(*) AS n FROM analytics_digest_intents")["n"] == 1


def test_digest_respects_studio_opt_out(app_env):
    _cid, listing_id, gallery_id = _seed_listing()
    _view("default", listing_id, gallery_id)
    tenant.set_studio("default")
    studio_mod.update_profile(analytics_digest_enabled=False)
    assert analytics.enqueue_weekly_digests() == 0
    assert db.one("SELECT COUNT(*) AS n FROM analytics_digest_intents")["n"] == 0


def test_digest_scopes_intents_per_studio(app_env):
    _cid, listing_id, gallery_id = _seed_listing()
    _view("default", listing_id, gallery_id)
    db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other', 'other')")
    _ocid, other_listing, other_gallery = _seed_listing(
        studio="other", title="999 Other St", email="other@example.com"
    )
    _view("other", other_listing, other_gallery)

    tenant.set_studio("default")
    assert analytics.enqueue_weekly_digests() == 2
    rows = db.all_("SELECT studio_id, to_email FROM analytics_digest_intents ORDER BY studio_id")
    assert [(r["studio_id"], r["to_email"]) for r in rows] == [
        ("default", "agent@example.com"),
        ("other", "other@example.com"),
    ]


@pytest.mark.asyncio
async def test_agent_portal_shows_view_counts(app_env):
    client_id, listing_id, gallery_id = _seed_listing()
    _view("default", listing_id, gallery_id)
    _view("default", listing_id, gallery_id, visitor="visitor-2")
    tenant.set_studio("default")
    token = portal.ensure_token(client_id)
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get(f"/portal/{token}")
        assert r.status_code == 200
        assert "2 views" in r.text
        assert "2 unique" in r.text


def test_portal_counts_empty_for_quiet_listings(app_env):
    _cid, listing_id, _gid = _seed_listing()
    tenant.set_studio("default")
    counts = analytics.portal_counts([listing_id], studio_id="default")
    assert counts[listing_id] == {"views": 0, "unique_visitors": 0}
    clients.get_client(_cid)
