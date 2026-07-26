from __future__ import annotations

import html
import importlib
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import eos.admin_oauth as admin_oauth
import eos.config as config
import eos.db as db
import eos.main as main
import eos.security as security
import eos.tenant as tenant
import eos.users as users
import httpx
import pytest


@pytest.fixture()
def google_login_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-google-login-secret-key-32!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_BASE_URL", "https://eos.test")
    monkeypatch.setenv("EOS_BASE_DOMAIN", "eos.test")
    monkeypatch.setenv("EOS_COOKIE_SECURE", "true")
    monkeypatch.setenv("EOS_SAAS_MODE", "false")
    monkeypatch.setenv("EOS_SIGNUP_ENABLED", "false")
    monkeypatch.setenv("EOS_BILLING_ENFORCE", "false")
    monkeypatch.setenv("EOS_GOOGLE_CLIENT_ID", "google-client")
    monkeypatch.setenv("EOS_GOOGLE_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv(
        "EOS_GOOGLE_ADMIN_REDIRECT_URI",
        "https://eos.test/oauth/google/admin/callback",
    )
    for module in (config, db, security, tenant, admin_oauth, main):
        importlib.reload(module)
    config.ensure_dirs()
    db.migrate()
    db.run(
        """INSERT INTO studio
           (id, name, slug, active, signup_verified, custom_domain,
            custom_domain_verified)
           VALUES ('alpha','Alpha Studio','alpha',1,1,'photos.example.test',1)"""
    )
    db.run(
        """INSERT INTO studio (id, name, slug, active, signup_verified)
           VALUES ('beta','Beta Studio','beta',1,1)"""
    )
    for studio_id, email in (
        ("alpha", "owner@alpha.test"),
        ("beta", "owner@beta.test"),
    ):
        db.run(
            """INSERT INTO users
               (studio_id, email, password_hash, name, role, active)
               VALUES (?,?,?,?, 'owner', 1)""",
            (studio_id, email, users.hash_password("password"), "Owner"),
        )
    tenant.set_studio("default")
    return main.app


def _state_from_url(location: str) -> str:
    url = urllib.parse.urlsplit(html.unescape(location))
    return urllib.parse.parse_qs(url.query)["state"][0]


def _token_from_redirect(location: str) -> str:
    parsed = urllib.parse.urlsplit(location)
    assert "handoff" not in urllib.parse.parse_qs(parsed.query)
    return urllib.parse.parse_qs(parsed.fragment)["handoff"][0]


def _completion_headers(host: str) -> dict[str, str]:
    return {
        "Origin": f"https://{host}",
        "Sec-Fetch-Site": "same-origin",
    }


def _cookie_value(client: httpx.AsyncClient, name: str, domain: str) -> str:
    value = client.cookies.get(name, domain=domain, path="/")
    assert value
    return value


async def _begin_http(client: httpx.AsyncClient, host: str) -> tuple[str, str, httpx.Response]:
    login = await client.get(f"https://{host}/admin/login")
    assert login.status_code == 200
    assert 'href="/oauth/google/admin/start"' in login.text
    assert db.one("SELECT COUNT(*) AS n FROM google_admin_login_flows")["n"] == 0
    response = await client.get(
        f"https://{host}/oauth/google/admin/start",
        follow_redirects=False,
    )
    assert response.status_code == 303
    state = _state_from_url(response.headers["location"])
    nonce = _cookie_value(client, admin_oauth.NONCE_COOKIE, host)
    return state, nonce, response


async def _callback_http(
    client: httpx.AsyncClient,
    monkeypatch,
    state: str,
    email: str,
) -> httpx.Response:
    monkeypatch.setattr(
        admin_oauth,
        "_provider_profile",
        lambda code: {"email": email, "email_verified": True},
    )
    return await client.get(
        "https://eos.test/oauth/google/admin/callback",
        params={"code": "google-code", "state": state},
        follow_redirects=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["alpha.eos.test", "photos.example.test"])
async def test_subdomain_and_custom_domain_login_complete_only_on_tenant(
    google_login_env,
    monkeypatch,
    host,
):
    transport = httpx.ASGITransport(app=google_login_env)
    async with httpx.AsyncClient(transport=transport, base_url="https://eos.test") as client:
        state, nonce, login = await _begin_http(client, host)
        login_cookies = login.headers.get_list("set-cookie")
        nonce_cookie = next(c for c in login_cookies if admin_oauth.NONCE_COOKIE in c)
        assert "Secure" in nonce_cookie
        assert "HttpOnly" in nonce_cookie
        assert "SameSite=lax" in nonce_cookie
        assert "Domain=" not in nonce_cookie

        stored = db.one("SELECT * FROM google_admin_login_flows ORDER BY id DESC LIMIT 1")
        assert stored["state_hash"] == admin_oauth._fingerprint(state)
        assert stored["state_hash"] != state
        assert stored["browser_nonce_hash"] == admin_oauth._fingerprint(nonce)
        assert stored["browser_nonce_hash"] != nonce
        assert stored["return_host"] == host

        callback = await _callback_http(client, monkeypatch, state, "owner@alpha.test")
        assert callback.status_code == 303
        assert security.ADMIN_COOKIE not in callback.headers.get("set-cookie", "")
        location = callback.headers["location"]
        parsed = urllib.parse.urlsplit(location)
        assert parsed.netloc == host
        assert parsed.path == "/oauth/google/admin/complete"
        token = _token_from_redirect(location)
        completed = db.one("SELECT * FROM google_admin_login_flows WHERE id=?", (stored["id"],))
        assert completed["completion_token_hash"] == admin_oauth._fingerprint(token)
        assert completed["completion_token_hash"] != token

        page = await client.get(urllib.parse.urlunsplit(parsed._replace(fragment="")))
        assert page.status_code == 200
        assert page.headers["cache-control"] == "private, no-store"
        assert page.headers["referrer-policy"] == "no-referrer"
        assert '<meta name="referrer" content="no-referrer">' in page.text
        assert token not in page.text
        assert "window.history.replaceState" in page.text
        assert 'referrerPolicy: "no-referrer"' in page.text

        complete = await client.post(
            f"https://{host}/oauth/google/admin/complete",
            json={"handoff": token},
            headers=_completion_headers(host),
        )
        assert complete.status_code == 200
        assert complete.json() == {"redirect": "/admin"}
        set_cookies = complete.headers.get_list("set-cookie")
        admin_cookie = next(c for c in set_cookies if c.startswith(f"{security.ADMIN_COOKIE}="))
        csrf_cookie = next(c for c in set_cookies if c.startswith(f"{security.CSRF_COOKIE}="))
        for cookie in (admin_cookie, csrf_cookie):
            assert "Secure" in cookie
            assert "SameSite=lax" in cookie
            assert "Domain=" not in cookie
        assert "HttpOnly" in admin_cookie

        assert _cookie_value(client, security.ADMIN_COOKIE, host)
        assert client.cookies.get(admin_oauth.NONCE_COOKIE, domain=host, path="/") is None
        assert client.cookies.get(security.ADMIN_COOKIE, domain="eos.test", path="/") is None
        assert client.cookies.get(security.ADMIN_COOKIE, domain="beta.eos.test", path="/") is None
        apex = await client.get("https://eos.test/admin", follow_redirects=False)
        sibling = await client.get("https://beta.eos.test/admin", follow_redirects=False)
        assert apex.status_code == 303
        assert sibling.status_code == 303
        assert apex.headers["location"] == "/admin/login"
        assert sibling.headers["location"] == "/admin/login"


@pytest.mark.asyncio
async def test_missing_wrong_nonce_cross_tenant_and_origin_do_not_consume(
    google_login_env,
    monkeypatch,
):
    transport = httpx.ASGITransport(app=google_login_env)
    async with httpx.AsyncClient(transport=transport, base_url="https://eos.test") as client:
        state, nonce, _ = await _begin_http(client, "alpha.eos.test")
        callback = await _callback_http(client, monkeypatch, state, "owner@alpha.test")
        token = _token_from_redirect(callback.headers["location"])

        assert (
            admin_oauth.consume_completion(
                token,
                browser_nonce="",
                studio_id="alpha",
                host="alpha.eos.test",
            )
            is None
        )
        assert (
            admin_oauth.consume_completion(
                token,
                browser_nonce="wrong",
                studio_id="alpha",
                host="alpha.eos.test",
            )
            is None
        )
        assert (
            admin_oauth.consume_completion(
                token,
                browser_nonce=nonce,
                studio_id="beta",
                host="beta.eos.test",
            )
            is None
        )
        bad_origin = await client.post(
            "https://alpha.eos.test/oauth/google/admin/complete",
            json={"handoff": token},
            headers={"Origin": "https://evil.test", "Sec-Fetch-Site": "cross-site"},
        )
        assert bad_origin.status_code == 403
        assert security.ADMIN_COOKIE not in bad_origin.headers.get("set-cookie", "")
        assert db.one("SELECT status FROM google_admin_login_flows")["status"] == "completed"

        success = await client.post(
            "https://alpha.eos.test/oauth/google/admin/complete",
            json={"handoff": token},
            headers=_completion_headers("alpha.eos.test"),
        )
        assert success.status_code == 200


def _prepare_completion(host: str, email: str, monkeypatch) -> tuple[str, str]:
    start = admin_oauth.begin_login(studio_id="alpha", host=host)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(start.url).query)["state"][0]
    monkeypatch.setattr(
        admin_oauth,
        "_provider_profile",
        lambda code: {"email": email, "email_verified": True},
    )
    result = admin_oauth.handle_callback("code", state)
    assert result and result["ok"]
    return _token_from_redirect(result["redirect_url"]), start.browser_nonce


def test_changed_host_inactive_user_expiry_and_replay_fail_closed(
    google_login_env,
    monkeypatch,
):
    token, nonce = _prepare_completion("alpha.eos.test", "owner@alpha.test", monkeypatch)
    db.run("UPDATE studio SET slug='alpha-renamed' WHERE id='alpha'")
    assert (
        admin_oauth.consume_completion(
            token,
            browser_nonce=nonce,
            studio_id="alpha",
            host="alpha.eos.test",
        )
        is None
    )

    db.run("UPDATE studio SET slug='alpha' WHERE id='alpha'")
    db.run("UPDATE users SET active=0 WHERE studio_id='alpha'")
    assert (
        admin_oauth.consume_completion(
            token,
            browser_nonce=nonce,
            studio_id="alpha",
            host="alpha.eos.test",
        )
        is None
    )
    db.run("UPDATE users SET active=1 WHERE studio_id='alpha'")
    db.run(
        "UPDATE google_admin_login_flows SET completion_expires_at=?",
        (admin_oauth._now() - 1,),
    )
    assert (
        admin_oauth.consume_completion(
            token,
            browser_nonce=nonce,
            studio_id="alpha",
            host="alpha.eos.test",
        )
        is None
    )

    fresh_token, fresh_nonce = _prepare_completion(
        "alpha.eos.test", "owner@alpha.test", monkeypatch
    )
    assert admin_oauth.consume_completion(
        fresh_token,
        browser_nonce=fresh_nonce,
        studio_id="alpha",
        host="alpha.eos.test",
    )
    assert (
        admin_oauth.consume_completion(
            fresh_token,
            browser_nonce=fresh_nonce,
            studio_id="alpha",
            host="alpha.eos.test",
        )
        is None
    )


def test_concurrent_completion_has_exactly_one_winner(google_login_env, monkeypatch):
    token, nonce = _prepare_completion("alpha.eos.test", "owner@alpha.test", monkeypatch)
    barrier = threading.Barrier(8)

    def consume():
        barrier.wait()
        return admin_oauth.consume_completion(
            token,
            browser_nonce=nonce,
            studio_id="alpha",
            host="alpha.eos.test",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: consume(), range(8)))
    assert sum(result is not None for result in results) == 1
    assert db.one("SELECT status FROM google_admin_login_flows")["status"] == "consumed"


def test_state_is_claimed_before_provider_io_and_only_one_callback_runs(
    google_login_env,
    monkeypatch,
):
    start = admin_oauth.begin_login(studio_id="alpha", host="alpha.eos.test")
    state = _state_from_url(start.url)
    provider_entered = threading.Event()
    release_provider = threading.Event()
    calls = []

    def provider(_code):
        calls.append("called")
        provider_entered.set()
        assert release_provider.wait(timeout=5)
        return {"email": "owner@alpha.test", "email_verified": True}

    monkeypatch.setattr(admin_oauth, "_provider_profile", provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(admin_oauth.handle_callback, "code", state)
        assert provider_entered.wait(timeout=5)
        second = pool.submit(admin_oauth.handle_callback, "code", state)
        assert second.result(timeout=5) is None
        release_provider.set()
        first_result = first.result(timeout=5)
    assert first_result and first_result["ok"]
    assert calls == ["called"]


def test_state_expiry_error_and_replay_never_call_provider(google_login_env, monkeypatch):
    expired = admin_oauth.begin_login(studio_id="alpha", host="alpha.eos.test")
    expired_state = urllib.parse.parse_qs(urllib.parse.urlsplit(expired.url).query)["state"][0]
    db.run("UPDATE google_admin_login_flows SET state_expires_at=?", (admin_oauth._now() - 1,))
    calls = []
    monkeypatch.setattr(admin_oauth, "_provider_profile", lambda code: calls.append(code))
    assert admin_oauth.handle_callback("code", expired_state) is None
    assert calls == []

    denied = admin_oauth.begin_login(studio_id="alpha", host="alpha.eos.test")
    denied_state = _state_from_url(denied.url)
    result = admin_oauth.handle_callback("", denied_state, provider_error="access_denied")
    assert result and not result["ok"]
    assert admin_oauth.handle_callback("code", denied_state) is None
    assert calls == []


def test_start_prunes_expired_and_old_terminal_flows(google_login_env):
    expired = admin_oauth.begin_login(studio_id="alpha", host="alpha.eos.test")
    expired_state = _state_from_url(expired.url)
    db.run(
        "UPDATE google_admin_login_flows SET state_expires_at=? WHERE state_hash=?",
        (admin_oauth._now() - 1, admin_oauth._fingerprint(expired_state)),
    )
    terminal = admin_oauth.begin_login(studio_id="alpha", host="alpha.eos.test")
    terminal_state = _state_from_url(terminal.url)
    db.run(
        """UPDATE google_admin_login_flows
           SET status='failed', updated_at=datetime('now', '-8 days')
           WHERE state_hash=?""",
        (admin_oauth._fingerprint(terminal_state),),
    )
    admin_oauth.begin_login(studio_id="alpha", host="alpha.eos.test")
    rows = db.all_("SELECT state_hash, status FROM google_admin_login_flows")
    assert len(rows) == 1
    assert rows[0]["status"] == "initiated"


@pytest.mark.asyncio
async def test_insecure_configuration_hides_and_rejects_google_start(
    google_login_env,
    monkeypatch,
):
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    transport = httpx.ASGITransport(app=google_login_env)
    async with httpx.AsyncClient(transport=transport, base_url="https://eos.test") as client:
        for _ in range(3):
            login = await client.get("https://alpha.eos.test/admin/login")
            assert 'href="/oauth/google/admin/start"' not in login.text
        assert db.one("SELECT COUNT(*) AS n FROM google_admin_login_flows")["n"] == 0
        start = await client.get(
            "https://alpha.eos.test/oauth/google/admin/start",
            follow_redirects=False,
        )
        assert start.status_code == 303
        assert start.headers["location"] == "/admin/login?oauth_error=google"
        assert admin_oauth.NONCE_COOKIE not in start.headers.get("set-cookie", "")
        assert db.one("SELECT COUNT(*) AS n FROM google_admin_login_flows")["n"] == 0

        monkeypatch.setattr(config, "COOKIE_SECURE", True)
        monkeypatch.setattr(
            config,
            "GOOGLE_ADMIN_REDIRECT_URI",
            "http://eos.test/oauth/google/admin/callback",
        )
        insecure_callback = await client.get("https://alpha.eos.test/admin/login")
        assert 'href="/oauth/google/admin/start"' not in insecure_callback.text

        monkeypatch.setattr(
            config,
            "GOOGLE_ADMIN_REDIRECT_URI",
            "https://eos.test/oauth/google/admin/callback",
        )
        insecure_request = await client.get("http://alpha.eos.test/admin/login")
        assert 'href="/oauth/google/admin/start"' not in insecure_request.text


def test_unexpected_provider_failure_marks_claim_failed(google_login_env, monkeypatch):
    start = admin_oauth.begin_login(studio_id="alpha", host="alpha.eos.test")
    state = _state_from_url(start.url)

    def provider_failure(_code):
        raise RuntimeError("unexpected provider adapter failure")

    monkeypatch.setattr(admin_oauth, "_provider_profile", provider_failure)
    result = admin_oauth.handle_callback("code", state)
    assert result and not result["ok"]
    row = db.one("SELECT status, error FROM google_admin_login_flows")
    assert dict(row) == {"status": "failed", "error": "provider exchange failed"}

    malformed = admin_oauth.begin_login(studio_id="alpha", host="alpha.eos.test")
    malformed_state = _state_from_url(malformed.url)
    monkeypatch.setattr(admin_oauth, "_provider_profile", lambda _code: [])
    result = admin_oauth.handle_callback("code", malformed_state)
    assert result and not result["ok"]
    row = db.one(
        """SELECT status, error FROM google_admin_login_flows
           WHERE state_hash=?""",
        (admin_oauth._fingerprint(malformed_state),),
    )
    assert dict(row) == {"status": "failed", "error": "provider exchange failed"}


@pytest.mark.asyncio
async def test_callback_requires_apex_and_claims_before_provider_io(
    google_login_env,
    monkeypatch,
):
    transport = httpx.ASGITransport(app=google_login_env)
    async with httpx.AsyncClient(transport=transport, base_url="https://eos.test") as client:
        state, _nonce, _ = await _begin_http(client, "alpha.eos.test")
        calls = []
        monkeypatch.setattr(
            admin_oauth,
            "_provider_profile",
            lambda code: (
                calls.append(code) or {"email": "owner@alpha.test", "email_verified": True}
            ),
        )
        wrong_host = await client.get(
            "https://alpha.eos.test/oauth/google/admin/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        assert wrong_host.status_code == 303
        assert calls == []
        assert db.one("SELECT status FROM google_admin_login_flows")["status"] == "initiated"

        first = await client.get(
            "https://eos.test/oauth/google/admin/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        second = await client.get(
            "https://eos.test/oauth/google/admin/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        assert first.status_code == second.status_code == 303
        assert len(calls) == 1
        assert security.ADMIN_COOKIE not in first.headers.get("set-cookie", "")
        assert security.ADMIN_COOKIE not in second.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_callback_and_completion_require_https(google_login_env, monkeypatch):
    transport = httpx.ASGITransport(app=google_login_env)
    async with httpx.AsyncClient(transport=transport, base_url="https://eos.test") as client:
        state, _nonce, _ = await _begin_http(client, "alpha.eos.test")
        calls = []
        monkeypatch.setattr(
            admin_oauth,
            "_provider_profile",
            lambda code: (
                calls.append(code) or {"email": "owner@alpha.test", "email_verified": True}
            ),
        )

        callback = await client.get(
            "http://eos.test/oauth/google/admin/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        page = await client.get("http://alpha.eos.test/oauth/google/admin/complete")
        complete = await client.post(
            "http://alpha.eos.test/oauth/google/admin/complete",
            json={"handoff": "unused"},
            headers=_completion_headers("alpha.eos.test"),
        )

    assert callback.status_code == 303
    assert page.status_code == 404
    assert complete.status_code == 403
    assert calls == []
    assert db.one("SELECT status FROM google_admin_login_flows")["status"] == "initiated"
