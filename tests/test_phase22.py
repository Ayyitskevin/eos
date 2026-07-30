"""Phase 22 — embeddable booking widget + property-site lead capture."""

import importlib
import re

import eos.config as config
import eos.db as db
import eos.demo_sandbox as demo_sandbox
import eos.leads as leads
import eos.mailer as mailer
import eos.main as main
import eos.onboarding as onboarding
import eos.rbac as rbac
import eos.security as security
import eos.studio as studio_mod
import eos.tenant as tenant
import eos.users as users
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request


def _enable_booking() -> None:
    tenant.set_studio("default")
    studio_mod.update_profile(booking_enabled=True)


@pytest.fixture(autouse=True)
def _reset_tenant_binding():
    """Never leak a tenant contextvar into later test modules."""
    yield
    tenant.set_studio("default")


def _form_bits(html: str) -> tuple[int, str]:
    package = re.search(r'name="package_id" value="(\d+)"', html)
    slot = re.search(r'<option value="([^"]+)"', html)
    assert package and slot
    return int(package.group(1)), slot.group(1)


def _booking_payload(package_id: int, scheduled_at: str, request_key: str) -> dict:
    return {
        "name": "Agent One",
        "email": "agent@example.com",
        "phone": "512-555-0100",
        "property_address": "101 Embed Way, Austin TX",
        "package_id": str(package_id),
        "scheduled_at": scheduled_at,
        "signer_name": "Agent One",
        "request_key": request_key,
        "embed": "1",
    }


def _seed_listing(
    studio: str = "default",
    title: str = "123 Main St",
    email: str = "agent@example.com",
    lead_capture: int = 1,
) -> tuple[int, int]:
    tenant.set_studio(studio)
    client_id = db.run(
        "INSERT INTO clients (studio_id, client_type, name, email) VALUES (?,?,?,?)",
        (studio, "agent", "Agent One", email),
    )
    listing_id = db.run(
        """INSERT INTO listings
           (studio_id, client_id, title, status, address_line1, site_slug,
            site_published, site_lead_capture)
           VALUES (?,?,?,'delivered',?,?,1,?)""",
        (
            studio,
            client_id,
            title,
            title,
            f"site-{studio}-{title.lower().replace(' ', '-')}",
            lead_capture,
        ),
    )
    return client_id, listing_id


def _lead_payload(**overrides) -> dict:
    data = {
        "name": "Buyer One",
        "email": "buyer@example.com",
        "phone": "512-555-0139",
        "message": "Is this still available?",
        "website": "",
    }
    data.update(overrides)
    return data


async def _admin_client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    login = await client.post(
        "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
    )
    assert login.status_code == 303
    return client


# --- Embeddable booking widget ----------------------------------------------


@pytest.mark.asyncio
async def test_embed_page_renders_chrome_free_and_frameable(app_env):
    _enable_booking()
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        embed = await client.get("/book/embed")
        assert embed.status_code == 200
        assert 'name="embed" value="1"' in embed.text
        assert "← Back" not in embed.text
        assert "site-main" not in embed.text
        assert embed.headers["content-security-policy"] == "frame-ancestors *"
        assert "x-frame-options" not in embed.headers

        page = await client.get("/book")
        assert page.status_code == 200
        assert page.headers["x-frame-options"] == "DENY"
        assert "content-security-policy" not in page.headers


@pytest.mark.asyncio
async def test_embed_blocked_when_studio_not_bookable(app_env):
    tenant.set_studio("default")
    studio_mod.update_profile(booking_enabled=False)
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        blocked_get = await client.get("/book/embed")
        blocked_post = await client.post(
            "/book",
            data={
                "name": "Blocked Agent",
                "email": "blocked@example.com",
                "property_address": "1 Closed Lane",
                "package_id": "1",
                "scheduled_at": "2099-01-01 10:00:00",
                "signer_name": "Blocked Agent",
                "request_key": "embed-not-bookable",
                "embed": "1",
            },
        )
    assert blocked_get.status_code == 404
    assert blocked_post.status_code == 404
    assert db.one("SELECT COUNT(*) AS n FROM inquiries WHERE studio_id='default'")["n"] == 0


@pytest.mark.asyncio
async def test_embed_frame_ancestors_follow_studio_settings(app_env):
    _enable_booking()
    studio_mod.update_profile(embed_allowed_domains="https://photos.mystudio.com, www.mystudio.com")
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        embed = await client.get("/book/embed")
    assert embed.headers["content-security-policy"] == (
        "frame-ancestors https://photos.mystudio.com www.mystudio.com"
    )


@pytest.mark.asyncio
async def test_embed_post_creates_booking_through_atomic_path(app_env):
    _enable_booking()
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get("/book/embed")
        package_id, slot = _form_bits(page.text)
        payload = _booking_payload(package_id, slot, "embed-replay-key")

        first = await client.post("/book", data=payload, follow_redirects=False)
        assert first.status_code == 303
        location = first.headers["location"]
        assert location.startswith("/booking/")
        assert location.endswith("?embed=1")

        # Replay with the same request key returns the same order, no duplicate.
        replay = await client.post("/book", data=payload, follow_redirects=False)
        assert replay.status_code == 303
        assert replay.headers["location"] == location
        rows = db.all_("SELECT * FROM inquiries WHERE studio_id='default'")
        assert len(rows) == 1
        assert rows[0]["request_key"] == "embed-replay-key"
        assert rows[0]["status"] in ("confirmed", "pending_payment")

        confirm = await client.get(location)
        assert confirm.status_code == 200
        assert "101 Embed Way" in confirm.text
        assert confirm.headers["content-security-policy"] == "frame-ancestors *"
        assert "x-frame-options" not in confirm.headers

        normal = await client.get(location.split("?")[0])
        assert normal.status_code == 200
        assert normal.headers["x-frame-options"] == "DENY"
        assert "content-security-policy" not in normal.headers


@pytest.mark.asyncio
async def test_embed_validation_error_renders_embed_variant(app_env):
    _enable_booking()
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get("/book/embed")
        package_id, slot = _form_bits(page.text)
        payload = _booking_payload(package_id, slot, "embed-bad-email")
        payload["email"] = "not-an-email"
        r = await client.post("/book", data=payload)
    assert r.status_code == 400
    assert "Invalid email." in r.text
    assert 'name="embed" value="1"' in r.text
    assert "site-main" not in r.text


@pytest.mark.asyncio
async def test_studio_settings_shows_embed_snippet(app_env):
    client = await _admin_client(app_env)
    try:
        r = await client.get("/admin/studio")
        assert r.status_code == 200
        assert "Embed booking widget" in r.text
        assert "/book/embed" in r.text
        assert 'name="embed_allowed_domains"' in r.text
        assert 'name="lead_capture_enabled"' in r.text
    finally:
        await client.aclose()


# --- SaaS tenant host --------------------------------------------------------


@pytest.fixture()
def saas_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_BASE_DOMAIN", "eos.test")
    monkeypatch.setenv("EOS_SAAS_MODE", "true")
    for mod in (config, db, security, tenant, onboarding, main):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    onboarding.create_studio(
        name="Alpha",
        slug="alpha",
        owner_email="a@alpha.test",
        owner_password="alpha-pass-1",
    )
    return main.app


@pytest.mark.asyncio
async def test_embed_on_tenant_host_respects_activation_gate(saas_env):
    transport = ASGITransport(app=saas_env)
    async with AsyncClient(transport=transport, base_url="http://eos.test") as client:
        headers = {"host": "alpha.eos.test"}
        blocked = await client.get("/book/embed", headers=headers)
        assert blocked.status_code == 404

        db.run("UPDATE studio SET signup_verified=1 WHERE id='alpha'")
        tenant.set_studio("alpha")
        studio_mod.update_profile(
            published=True,
            booking_enabled=True,
            headline="Bright, MLS-ready photos",
            service_area="Austin metro",
        )
        embed = await client.get("/book/embed", headers=headers)
        assert embed.status_code == 200
        assert 'name="embed" value="1"' in embed.text
        assert embed.headers["content-security-policy"] == "frame-ancestors *"
        assert "x-frame-options" not in embed.headers

        page = await client.get("/book", headers=headers)
        assert page.headers["x-frame-options"] == "DENY"


# --- Lead capture on property sites ------------------------------------------


@pytest.mark.asyncio
async def test_lead_submit_stores_tenant_scoped_lead(app_env):
    _client_id, listing_id = _seed_listing()
    slug = "site-default-123-main-st"
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        form = await client.get(f"/l/{slug}")
        assert form.status_code == 200
        assert "Interested in this property?" in form.text
        assert 'name="website"' in form.text  # honeypot present, visually hidden

        r = await client.post(f"/l/{slug}/inquire", data=_lead_payload(), follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/l/{slug}?thanks=1"

    lead = db.one("SELECT * FROM inquiries WHERE studio_id='default' AND status='inquiry'")
    assert lead["listing_id"] == listing_id
    assert lead["email"] == "buyer@example.com"
    assert lead["contacted"] == 0
    assert lead["ip_hash"]
    assert "." not in lead["ip_hash"]  # no raw IPv4 at rest

    intent = db.one("SELECT * FROM lead_notify_intents WHERE studio_id='default'")
    assert intent["status"] == "pending"
    assert intent["inquiry_id"] == lead["id"]
    assert intent["to_email"] == "agent@example.com"


@pytest.mark.asyncio
async def test_honeypot_drops_submission_silently(app_env):
    _seed_listing()
    slug = "site-default-123-main-st"
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            f"/l/{slug}/inquire",
            data=_lead_payload(website="http://spam.example"),
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == f"/l/{slug}?thanks=1"
    assert db.one("SELECT COUNT(*) AS n FROM inquiries WHERE status='inquiry'")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM lead_notify_intents")["n"] == 0


@pytest.mark.asyncio
async def test_lead_submit_rate_limited(app_env):
    _seed_listing()
    slug = "site-default-123-main-st"
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(security.INQUIRY_MAX_PER_WINDOW):
            r = await client.post(f"/l/{slug}/inquire", data=_lead_payload())
            assert r.status_code == 303
        limited = await client.post(f"/l/{slug}/inquire", data=_lead_payload())
        assert limited.status_code == 429
    n = db.one("SELECT COUNT(*) AS n FROM inquiries WHERE status='inquiry'")["n"]
    assert n == security.INQUIRY_MAX_PER_WINDOW


@pytest.mark.asyncio
async def test_lead_capture_toggles_hide_and_block_form(app_env):
    _seed_listing()
    _cid, other_listing = _seed_listing(title="456 Elm St", lead_capture=0)
    tenant.set_studio("default")
    studio_mod.update_profile(lead_capture_enabled=False)
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Studio-level toggle off: form hidden and POST rejected.
        page = await client.get("/l/site-default-123-main-st")
        assert page.status_code == 200
        assert "Interested in this property?" not in page.text
        blocked = await client.post("/l/site-default-123-main-st/inquire", data=_lead_payload())
        assert blocked.status_code == 404

        # Per-listing toggle off: same, even with the studio toggle back on.
        studio_mod.update_profile(lead_capture_enabled=True)
        page = await client.get("/l/site-default-456-elm-st")
        assert "Interested in this property?" not in page.text
        blocked = await client.post("/l/site-default-456-elm-st/inquire", data=_lead_payload())
        assert blocked.status_code == 404
    assert other_listing
    assert db.one("SELECT COUNT(*) AS n FROM inquiries WHERE status='inquiry'")["n"] == 0


@pytest.mark.asyncio
async def test_demo_read_only_studio_hides_and_blocks_leads(app_env, monkeypatch):
    _seed_listing()
    slug = "site-default-123-main-st"
    monkeypatch.setattr(demo_sandbox, "is_read_only", lambda *args, **kwargs: True)
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get(f"/l/{slug}")
        assert "Interested in this property?" not in page.text
        blocked = await client.post(f"/l/{slug}/inquire", data=_lead_payload())
        assert blocked.status_code == 404
    assert db.one("SELECT COUNT(*) AS n FROM inquiries WHERE status='inquiry'")["n"] == 0


def test_lead_notification_persisted_before_send(app_env, monkeypatch):
    _client_id, listing_id = _seed_listing()
    tenant.set_studio("default")
    listing = db.one("SELECT * FROM listings WHERE id=?", (listing_id,))
    leads.capture_lead(
        listing=listing,
        name="Buyer One",
        email="buyer@example.com",
        phone="",
        message="Showing this weekend?",
        ip="203.0.113.9",
        address="123 Main St",
    )
    # Mailer is not configured: the intent is still persisted first.
    intent = db.one("SELECT * FROM lead_notify_intents WHERE studio_id='default'")
    assert intent["status"] == "pending"
    assert leads.process_pending() == 0

    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(
        mailer, "send_for_studio", lambda to, subject, body: sent.append((to, subject, body))
    )
    assert leads.process_pending() == 1
    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == "agent@example.com"
    assert subject == "New property lead — 123 Main St"
    assert "buyer@example.com" in body
    assert "/l/site-default-123-main-st" in body

    intent = db.one("SELECT * FROM lead_notify_intents WHERE studio_id='default'")
    assert intent["status"] == "sent"
    assert db.one(
        "SELECT 1 AS x FROM emails_log WHERE studio_id='default' AND doc_kind='lead_notify'"
    )
    # Idempotent: nothing left to send.
    assert leads.process_pending() == 0
    assert len(sent) == 1


def test_leads_fall_back_to_studio_contact_without_agent(app_env):
    tenant.set_studio("default")
    db.run("UPDATE studio SET contact_email='studio@example.com' WHERE id='default'")
    listing_id = db.run(
        """INSERT INTO listings
           (studio_id, title, status, address_line1, site_slug, site_published,
            site_lead_capture)
           VALUES ('default','789 Oak St','delivered','789 Oak St','site-default-789-oak-st',1,1)"""
    )
    listing = db.one("SELECT * FROM listings WHERE id=?", (listing_id,))
    leads.capture_lead(
        listing=listing,
        name="Buyer Two",
        email="b2@example.com",
        phone="",
        message="",
        ip="203.0.113.10",
        address="789 Oak St",
    )
    intent = db.one("SELECT * FROM lead_notify_intents WHERE studio_id='default'")
    assert intent["to_email"] == "studio@example.com"


@pytest.mark.asyncio
async def test_leads_inbox_csv_and_contacted_toggle(app_env):
    _seed_listing()
    slug = "site-default-123-main-st"
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as public:
        r = await public.post(f"/l/{slug}/inquire", data=_lead_payload())
        assert r.status_code == 303

    client = await _admin_client(app_env)
    try:
        inbox = await client.get("/admin/listings/leads")
        assert inbox.status_code == 200
        assert "Property leads" in inbox.text
        assert "buyer@example.com" in inbox.text
        assert "123 Main St" in inbox.text

        csv_resp = await client.get("/admin/listings/leads.csv")
        assert csv_resp.status_code == 200
        assert csv_resp.headers["content-type"].startswith("text/csv")
        assert "buyer@example.com" in csv_resp.text
        assert "contacted" in csv_resp.text

        lead = db.one("SELECT * FROM inquiries WHERE status='inquiry'")
        csrf = client.cookies.get(security.CSRF_COOKIE)
        toggle = await client.post(
            f"/admin/listings/leads/{lead['id']}/contacted",
            data={"contacted": "1"},
            headers={"x-eos-csrf": csrf},
            follow_redirects=False,
        )
        assert toggle.status_code == 303
        assert db.one("SELECT contacted FROM inquiries WHERE id=?", (lead["id"],))["contacted"] == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_leads_inbox_requires_admin(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/admin/listings/leads", follow_redirects=False)
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


def test_leads_rbac_fail_closed(app_env):
    tenant.set_studio("default")
    scheduler_id = users.create_user("sched@example.com", "pass12345", role="scheduler")
    accountant_id = users.create_user("acct2@example.com", "pass12345", role="accountant")
    for path in ("/admin/listings/leads", "/admin/listings/leads.csv"):
        rbac.check_route(_role_request(scheduler_id, "GET", path))
        rbac.check_route(_role_request(accountant_id, "GET", path))
    # Marking leads contacted is a listings write — schedulers may, accountants may not.
    rbac.check_route(_role_request(scheduler_id, "POST", "/admin/listings/leads/1/contacted"))
    with pytest.raises(HTTPException) as exc_info:
        rbac.check_route(_role_request(accountant_id, "POST", "/admin/listings/leads/1/contacted"))
    assert exc_info.value.status_code == 403


def test_leads_are_scoped_per_studio(app_env):
    _seed_listing()
    db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other', 'other')")
    _ocid, other_listing = _seed_listing(
        studio="other", title="999 Other St", email="other@example.com"
    )
    other = db.one("SELECT * FROM listings WHERE id=?", (other_listing,))
    tenant.set_studio("other")
    leads.capture_lead(
        listing=other,
        name="Buyer Two",
        email="b2@example.com",
        phone="",
        message="",
        ip="203.0.113.11",
        address="999 Other St",
    )

    tenant.set_studio("default")
    default_listing = db.one("SELECT * FROM listings WHERE title='123 Main St'")
    leads.capture_lead(
        listing=default_listing,
        name="Buyer One",
        email="buyer@example.com",
        phone="",
        message="",
        ip="203.0.113.12",
        address="123 Main St",
    )
    default_leads = leads.list_leads()
    assert [lead["email"] for lead in default_leads] == ["buyer@example.com"]
    assert "b2@example.com" not in leads.leads_csv()

    tenant.set_studio("other")
    other_leads = leads.list_leads()
    assert [lead["email"] for lead in other_leads] == ["b2@example.com"]
    intent = db.one("SELECT * FROM lead_notify_intents WHERE studio_id='other'")
    assert intent["to_email"] == "other@example.com"
