"""Phase 14 — self-reschedule, credits, RBAC, churn, media paths."""

import importlib

import eos.churn as churn
import eos.clients as clients
import eos.config as config
import eos.credits as credits
import eos.db as db
import eos.jobs as jobs
import eos.listing_media as listing_media
import eos.media_paths as media_paths
import eos.rbac as rbac
import eos.reschedule as reschedule
import eos.security as security
import eos.tenant as tenant
import pytest
from fastapi import HTTPException
from starlette.requests import Request


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    for mod in (
        config,
        db,
        jobs,
        tenant,
        credits,
        clients,
        reschedule,
        churn,
        media_paths,
        security,
        listing_media,
        rbac,
    ):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    tenant.set_studio("default")
    yield
    jobs.stop()


def test_credit_balance_and_apply(env):
    cid = clients.create_client("Agent", email="a@test.com", client_type="agent")
    credits.add_credit(cid, amount_cents=5000, note="loyalty")
    assert credits.balance(cid) == 5000
    new_total, applied = credits.apply_at_checkout(cid, 12000)
    assert applied == 5000
    assert new_total == 7000
    assert credits.balance(cid) == 0


def test_media_paths_namespaced(env):
    tenant.set_studio("default")
    path = media_paths.gallery_dir(42)
    assert "default" in str(path) or path.name == "42"


def test_rbac_scheduler_perms(env):
    assert rbac.has_perm("scheduler", "calendar")
    assert not rbac.has_perm("scheduler", "reports")
    assert rbac.has_perm("owner", "reports")


def test_churn_inactive_agents(env):
    tenant.set_studio("default")
    clients.create_client("Old Agent", email="old@test.com", client_type="agent")
    rows = churn.inactive_agents(days=90)
    assert any(r["email"] == "old@test.com" for r in rows)


def test_reschedule_slots(env):
    from eos import scheduling

    tenant.set_studio("default")
    slots = scheduling.reschedule_slots(days=7)
    assert isinstance(slots, list)


def test_users_extended_roles(env):
    from eos import users

    tenant.set_studio("default")
    uid = users.create_user("sched@test.com", "pass12345", role="scheduler")
    row = users.get_user(uid)
    assert row["role"] == "scheduler"


def _role_user(role: str) -> int:
    return db.run(
        """INSERT INTO users (studio_id, email, password_hash, name, role)
           VALUES ('default', ?, 'test', ?, ?)""",
        (f"{role}-{db.one('SELECT COUNT(*) AS n FROM users')['n']}@test.example", role, role),
    )


def _admin_request(user_id: int | None, method: str, path: str) -> Request:
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
            "server": ("eos.test", 443),
            "client": ("127.0.0.1", 12345),
        }
    )


@pytest.mark.parametrize(
    ("role", "method", "path"),
    [
        ("accountant", "GET", "/admin"),
        ("accountant", "GET", "/admin/clients/1"),
        ("accountant", "GET", "/admin/listings/1"),
        ("accountant", "GET", "/admin/reports"),
        ("accountant", "GET", "/admin/reports/export.csv"),
        ("accountant", "GET", "/admin/brokerages"),
        ("accountant", "GET", "/admin/invoices/1"),
        ("accountant", "POST", "/admin/listings/1/invoice"),
        ("accountant", "POST", "/admin/invoices/1/send"),
        ("operator", "POST", "/admin/clients"),
        ("operator", "POST", "/admin/listings/1/media"),
        ("operator", "POST", "/admin/appointments"),
        ("operator", "POST", "/admin/galleries/1/settings"),
        ("operator", "GET", "/admin/reports/acquisition"),
        ("operator", "GET", "/admin/proposals/1"),
        ("scheduler", "POST", "/admin/appointments/1/reschedule"),
        ("scheduler", "POST", "/admin/listings/1/media"),
        ("editor", "POST", "/admin/listings/1/gallery"),
        ("editor", "GET", "/admin/galleries/1"),
    ],
)
def test_declared_role_routes_are_allowed(env, role, method, path):
    rbac.check_route(_admin_request(_role_user(role), method, path))


@pytest.mark.parametrize(
    ("role", "method", "path"),
    [
        ("accountant", "POST", "/admin/listings"),
        ("accountant", "POST", "/admin/listings/1/media"),
        ("accountant", "POST", "/admin/listings/1/media/2/delete"),
        ("accountant", "POST", "/admin/clients/1"),
        ("accountant", "POST", "/admin/clients/1/credit"),
        ("accountant", "POST", "/admin/appointments"),
        ("accountant", "POST", "/admin/galleries/1/settings"),
        ("accountant", "GET", "/admin/stripe/connect"),
        ("accountant", "POST", "/admin/stripe/connect/start"),
        ("accountant", "GET", "/admin/integrations/google/connect"),
        ("accountant", "POST", "/admin/integrations/dropbox/scan"),
        ("accountant", "GET", "/admin/studio"),
        ("accountant", "POST", "/admin/studio/api-tokens"),
        ("accountant", "POST", "/admin/studio/webhooks"),
        ("accountant", "POST", "/admin/email/invoices/1"),
        ("accountant", "POST", "/admin/reports/acquisition/bulk-send"),
        ("operator", "POST", "/admin/clients/1/credit"),
        ("operator", "POST", "/admin/billing/checkout"),
        ("operator", "POST", "/admin/rebooking/1/send"),
        ("operator", "POST", "/admin/listings/1/proposals"),
        ("editor", "POST", "/admin/contracts/1"),
        ("editor", "POST", "/admin/listings/1/invoice"),
        ("scheduler", "POST", "/admin/listings/1/gallery"),
        ("scheduler", "GET", "/admin/reports"),
        ("editor", "GET", "/admin/calendar"),
        ("accountant", "POST", "/admin/future-sensitive-action"),
        ("operator", "GET", "/admin/future-area"),
    ],
)
def test_non_owner_routes_fail_closed(env, role, method, path):
    with pytest.raises(HTTPException) as exc_info:
        rbac.check_route(_admin_request(_role_user(role), method, path))
    assert exc_info.value.status_code == 403


def test_owner_and_auth_lifecycle_routes_remain_accessible(env):
    owner_id = _role_user("owner")
    for method, path in (
        ("POST", "/admin/future-sensitive-action"),
        ("POST", "/admin/studio/api-tokens"),
        ("POST", "/admin/billing/checkout"),
    ):
        rbac.check_route(_admin_request(owner_id, method, path))
        rbac.check_route(_admin_request(None, method, path))

    accountant_id = _role_user("accountant")
    rbac.check_route(_admin_request(accountant_id, "GET", "/admin/login"))
    rbac.check_route(_admin_request(accountant_id, "POST", "/admin/logout"))


def test_invalid_authenticated_user_never_inherits_owner_access(env):
    user_id = _role_user("accountant")
    db.run("UPDATE users SET active=0 WHERE id=?", (user_id,))
    request = _admin_request(user_id, "GET", "/admin/clients")
    assert rbac.role_for_request(request) == "invalid"
    with pytest.raises(HTTPException) as exc_info:
        rbac.check_route(request)
    assert exc_info.value.status_code == 403


def _media_listing() -> int:
    return db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Media Test', 'booked')"
    )


@pytest.mark.parametrize(
    ("kind", "url", "iframe"),
    [
        ("youtube", "https://www.youtube.com/embed/demo?rel=0", True),
        ("youtube", "https://www.youtube-nocookie.com/embed/demo", True),
        ("vimeo", "https://player.vimeo.com/video/123456", True),
        ("matterport", "https://my.matterport.com/show/?m=Model_123", True),
        ("url", "https://www.youtube.com/watch?v=demo", False),
        ("url", "https://example.test/virtual-tour", False),
        ("iguide", "https://goiguide.com/example-tour", False),
    ],
)
def test_listing_media_accepts_only_declared_iframe_providers(env, kind, url, iframe):
    listing_id = _media_listing()
    media_id = listing_media.add_embed(
        listing_id,
        kind=kind,
        label="Tour",
        embed_url=url,
    )

    item = listing_media.list_for_listing(listing_id)[0]
    assert item["id"] == media_id
    assert item["render_url"] == url
    assert item["iframe"] is iframe


@pytest.mark.parametrize(
    ("kind", "url"),
    [
        ("url", "javascript:alert(1)"),
        ("url", "data:text/html,unsafe"),
        ("url", "http://example.test/tour"),
        ("url", "//example.test/tour"),
        ("url", "https://www.youtube.com.evil.test/watch?v=demo"),
        ("url", "https://youtube.com@evil.test/tour"),
        ("url", "https://example.test:444/tour"),
        ("url", "https://example.test\\@evil.test/tour"),
        ("youtube", "https://youtube.com/embed/demo"),
        ("youtube", "https://www.youtube.com/watch?v=demo"),
        ("youtube", "https://www.youtube.com.evil.test/embed/demo"),
        ("vimeo", "https://vimeo.com/123456"),
        ("vimeo", "https://player.vimeo.com/video/not-a-number"),
        ("matterport", "https://my.matterport.com/show/"),
        ("matterport", "https://my.matterport.com.evil.test/show/?m=Model_123"),
    ],
)
def test_listing_media_rejects_unsafe_or_noncanonical_urls(env, kind, url):
    listing_id = _media_listing()
    with pytest.raises(HTTPException) as exc_info:
        listing_media.add_embed(listing_id, kind=kind, label="Unsafe", embed_url=url)
    assert exc_info.value.status_code == 400
    assert db.one("SELECT COUNT(*) AS n FROM listing_media")["n"] == 0


def test_legacy_unsafe_media_is_never_rendered_and_iframes_are_sandboxed(env):
    from eos.render import templates

    listing_id = _media_listing()
    rows = (
        ("youtube", "Unsafe provider", "javascript:alert(1)"),
        ("url", "Unsafe link", "data:text/html,unsafe"),
        ("url", "Safe link", "https://example.test/tour"),
        ("youtube", "Safe video", "https://www.youtube.com/embed/demo"),
    )
    for kind, label, url in rows:
        db.run(
            """INSERT INTO listing_media (studio_id, listing_id, kind, label, embed_url)
               VALUES ('default', ?, ?, ?, ?)""",
            (listing_id, kind, label, url),
        )

    embeds = listing_media.list_for_listing(listing_id)
    assert [item["render_url"] for item in embeds[:2]] == [None, None]
    assert embeds[2]["iframe"] is False
    assert embeds[3]["iframe"] is True

    html = templates.env.get_template("public/gallery.html").render(
        g={
            "title": "Media Gallery",
            "cover_asset_id": None,
            "client_name": None,
            "slug": "media-gallery",
        },
        embeds=embeds,
        payment_locked=False,
        sections=[],
        by_section={},
        unsectioned=[],
        assets=[],
        upsell_addons=[],
        upsell=None,
        favorites=[],
    )
    assert "javascript:" not in html
    assert "data:text/html" not in html
    assert 'href="https://example.test/tour"' in html
    assert '<iframe src="https://example.test/tour"' not in html
    assert '<iframe src="https://www.youtube.com/embed/demo"' in html
    assert 'sandbox="allow-scripts allow-same-origin allow-presentation"' in html

    listing_source = templates.env.loader.get_source(templates.env, "public/listing_site.html")[0]
    assert "e.render_url" in listing_source
    assert 'sandbox="allow-scripts allow-same-origin allow-presentation"' in listing_source
