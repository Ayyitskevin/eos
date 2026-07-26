"""Authorized signup verification delivery reconciliation."""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import eos.billing_gate as billing_gate
import eos.config as config
import eos.db as db
import eos.mailer as mailer
import eos.main as main
import eos.rbac as rbac
import eos.routes.onboarding_routes as onboarding_routes
import eos.security as security
import eos.signup_verify as signup_verify
import eos.tenant as tenant
import eos.users as users
import pytest
from httpx import ASGITransport, AsyncClient

BASE_DOMAIN = "eos.test"
SIGNUP_PASSWORD = "test-password-2026"


@pytest.fixture()
def pending_http_env(tmp_path, monkeypatch):
    values = {
        "EOS_DATA_DIR": str(tmp_path / "data"),
        "EOS_SECRET_KEY": "test-secret-key-32chars-minimum!!",
        "EOS_ADMIN_PASSWORD": "test-admin-pass",
        "EOS_SAAS_MODE": "true",
        "EOS_SIGNUP_ENABLED": "true",
        "EOS_SIGNUP_AUTO_VERIFY_LOCAL": "false",
        "EOS_BASE_DOMAIN": BASE_DOMAIN,
        "EOS_BASE_URL": f"http://{BASE_DOMAIN}",
        "EOS_COOKIE_SECURE": "false",
        "EOS_BILLING_ENFORCE": "false",
        "EOS_DEMO_ENABLED": "false",
        "EOS_EMAIL_PROVIDER": "postmark",
        "EOS_POSTMARK_API_KEY": "pm-test-key",
        "EOS_POSTMARK_FROM_EMAIL": "notify@eos.test",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for module in (
        config,
        db,
        security,
        tenant,
        mailer,
        signup_verify,
        billing_gate,
        rbac,
        onboarding_routes,
        users,
        main,
    ):
        importlib.reload(module)
    config.ensure_dirs()
    db.migrate()
    tenant.set_studio("default")
    return main.app


def _seed_unknown(studio_id: str) -> dict[str, str | int]:
    email = f"owner@{studio_id}.test"
    password = f"{studio_id}-owner-pass-2026"
    token = f"verification-token-{studio_id}"
    db.run(
        """INSERT INTO studio
           (id, name, slug, contact_email, active, signup_verified,
            signup_verify_token, signup_verify_issued_at,
            provisioning_status, provisioning_error)
           VALUES (?,?,?,?,1,0,?,datetime('now'),'degraded',?)""",
        (
            studio_id,
            f"{studio_id.title()} Studio",
            studio_id,
            email,
            token,
            "verification: provider outcome unknown",
        ),
    )
    db.run(
        "INSERT INTO studio_profiles (studio_id) VALUES (?)",
        (studio_id,),
    )
    owner_id = db.run(
        """INSERT INTO users (studio_id, email, password_hash, name, role)
           VALUES (?,?,?,?, 'owner')""",
        (studio_id, email, users.hash_password(password), "Owner"),
    )
    db.run(
        """INSERT INTO signup_verification_intents
           (studio_id, token_fingerprint, to_email, status, error)
           VALUES (?,?,?,'unknown','provider outcome unknown')""",
        (studio_id, token[-12:], email),
    )
    return {
        "studio_id": studio_id,
        "email": email,
        "password": password,
        "owner_id": owner_id,
    }


async def _login(client: AsyncClient, account: dict[str, str | int]) -> None:
    response = await client.post(
        "/admin/login",
        data={"email": account["email"], "password": account["password"]},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert client.cookies.get(security.ADMIN_COOKIE)


def _signup_payload(slug: str, *, invite_code: str = "") -> dict[str, str]:
    return {
        "studio_name": f"{slug.title()} Studio",
        "slug": slug,
        "owner_name": "Owner",
        "owner_email": f"owner@{slug}.test",
        "owner_password": SIGNUP_PASSWORD,
        "invite_code": invite_code,
    }


def _signup_attempt_count(ip: str) -> int:
    row = db.one(
        """SELECT COUNT(*) AS n FROM pin_attempts
           WHERE ip=? AND gallery_id=?""",
        (ip, security.SIGNUP_BUCKET),
    )
    return int(row["n"])


@pytest.mark.asyncio
async def test_signup_claim_precedes_request_body_validation(pending_http_env):
    ip = "198.51.100.45"
    transport = ASGITransport(app=pending_http_env)
    async with AsyncClient(
        transport=transport,
        base_url=f"http://{BASE_DOMAIN}",
    ) as client:
        response = await client.post(
            "/signup",
            headers={security.CLIENT_IP_HEADER: ip},
            data={},
        )

    assert response.status_code == 422
    assert _signup_attempt_count(ip) == 1


@pytest.mark.asyncio
async def test_hosted_signup_without_mail_configuration_remains_unverified(
    pending_http_env,
    monkeypatch,
):
    monkeypatch.setattr(signup_verify.mailer, "configured", lambda: False)
    provider = Mock()
    monkeypatch.setattr(signup_verify.mailer, "send_platform", provider)
    transport = ASGITransport(app=pending_http_env)
    async with AsyncClient(
        transport=transport,
        base_url=f"http://{BASE_DOMAIN}",
    ) as client:
        response = await client.post(
            "/signup",
            data={
                "studio_name": "No Mail Studio",
                "slug": "no-mail",
                "owner_name": "Owner",
                "owner_email": "owner@no-mail.test",
                "owner_password": "test-password-2026",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "http://no-mail.eos.test/admin/login"
    studio = db.one(
        """SELECT signup_verified, signup_verify_token, signup_verify_issued_at,
                  provisioning_status, provisioning_error
           FROM studio WHERE id='no-mail'"""
    )
    assert studio["signup_verified"] == 0
    assert studio["signup_verify_token"]
    assert studio["signup_verify_issued_at"]
    assert studio["provisioning_status"] == "degraded"
    assert "hosted signup remains unverified" in studio["provisioning_error"]
    assert (
        db.one(
            """SELECT 1 AS x FROM signup_verification_intents
           WHERE studio_id='no-mail'"""
        )
        is None
    )
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_signup_rate_claim_counts_invalid_duplicate_provider_failure_and_success(
    pending_http_env,
    monkeypatch,
):
    ip = "198.51.100.44"
    monkeypatch.setattr(config, "SIGNUP_INVITE_ONLY", True)
    monkeypatch.setattr(config, "SIGNUP_RATE_LIMIT", 4)
    monkeypatch.setattr(config, "SIGNUP_RATE_WINDOW_SEC", 3600)
    db.run("INSERT INTO invite_codes (code, label, max_uses) VALUES ('BETA2026','test',10)")

    def provider(to: str, *_args) -> None:
        if to == "owner@provider-fail.test":
            raise RuntimeError("provider rejected before dispatch")

    monkeypatch.setattr(mailer, "send_platform", provider)
    headers = {security.CLIENT_IP_HEADER: ip}
    transport = ASGITransport(app=pending_http_env)
    async with AsyncClient(
        transport=transport,
        base_url=f"http://{BASE_DOMAIN}",
    ) as client:
        invalid_invite = await client.post(
            "/signup",
            headers=headers,
            data=_signup_payload("invalid-invite", invite_code="WRONG"),
            follow_redirects=False,
        )
        assert invalid_invite.status_code == 400
        assert _signup_attempt_count(ip) == 1

        success = await client.post(
            "/signup",
            headers=headers,
            data=_signup_payload("sequential-ok", invite_code="BETA2026"),
            follow_redirects=False,
        )
        assert success.status_code == 303
        assert success.headers["location"] == (f"http://sequential-ok.{BASE_DOMAIN}/admin/login")
        assert "cap=" not in success.headers["location"]
        assert _signup_attempt_count(ip) == 2

        duplicate = await client.post(
            "/signup",
            headers=headers,
            data=_signup_payload("sequential-ok", invite_code="BETA2026"),
            follow_redirects=False,
        )
        assert duplicate.status_code == 409
        assert _signup_attempt_count(ip) == 3

        provider_failure = await client.post(
            "/signup",
            headers=headers,
            data=_signup_payload("provider-fail", invite_code="BETA2026"),
            follow_redirects=False,
        )
        assert provider_failure.status_code == 303
        assert _signup_attempt_count(ip) == 4

        over_limit = await client.post(
            "/signup",
            headers=headers,
            data=_signup_payload("sequential-blocked", invite_code="BETA2026"),
            follow_redirects=False,
        )

    assert over_limit.status_code == 429
    assert _signup_attempt_count(ip) == 4
    assert db.one("SELECT id FROM studio WHERE id='provider-fail'")
    assert not db.one("SELECT id FROM studio WHERE id='sequential-blocked'")


@pytest.mark.asyncio
async def test_concurrent_signup_rate_boundary_never_overadmits(
    pending_http_env,
    monkeypatch,
):
    ip = "203.0.113.77"
    monkeypatch.setattr(config, "SIGNUP_INVITE_ONLY", False)
    monkeypatch.setattr(config, "SIGNUP_RATE_LIMIT", 2)
    monkeypatch.setattr(config, "SIGNUP_RATE_WINDOW_SEC", 3600)
    monkeypatch.setattr(mailer, "send_platform", lambda *_args: None)
    transport = ASGITransport(app=pending_http_env)

    async def submit(index: int):
        async with AsyncClient(
            transport=transport,
            base_url=f"http://{BASE_DOMAIN}",
        ) as client:
            return await client.post(
                "/signup",
                headers={security.CLIENT_IP_HEADER: ip},
                data=_signup_payload(f"race-{index}"),
                follow_redirects=False,
            )

    responses = await asyncio.gather(*(submit(index) for index in range(3)))

    assert sorted(response.status_code for response in responses) == [303, 303, 429]
    assert _signup_attempt_count(ip) == 2
    assert db.one("SELECT COUNT(*) AS n FROM studio WHERE id LIKE 'race-%'")["n"] == 2
    for response in responses:
        if response.status_code == 303:
            assert response.headers["location"].endswith("/admin/login")
            assert "cap=" not in response.headers["location"]

    blocked = await submit(99)
    assert blocked.status_code == 429
    assert _signup_attempt_count(ip) == 2
    assert not db.one("SELECT id FROM studio WHERE id='race-99'")


@pytest.mark.asyncio
async def test_reconciliation_requires_active_matching_owner_and_csrf(
    pending_http_env,
):
    alpha = _seed_unknown("alpha")
    beta = _seed_unknown("beta")
    operator_password = "alpha-operator-pass-2026"
    operator_id = db.run(
        """INSERT INTO users (studio_id, email, password_hash, name, role)
           VALUES ('alpha','operator@alpha.test',?,'Operator','operator')""",
        (users.hash_password(operator_password),),
    )
    inactive_owner_id = db.run(
        """INSERT INTO users
           (studio_id, email, password_hash, name, role, active)
           VALUES ('alpha','inactive@alpha.test',?,'Inactive Owner','owner',0)""",
        (users.hash_password("inactive-owner-pass-2026"),),
    )
    transport = ASGITransport(app=pending_http_env)

    async with AsyncClient(
        transport=transport,
        base_url=f"http://alpha.{BASE_DOMAIN}",
    ) as anonymous:
        rejected = await anonymous.get("/admin/verify-pending", follow_redirects=False)
        assert rejected.status_code == 303
        assert rejected.headers["location"] == "/admin/login"

    async with AsyncClient(
        transport=transport,
        base_url=f"http://alpha.{BASE_DOMAIN}",
        cookies={security.ADMIN_COOKIE: security.set_session_cookie(int(beta["owner_id"]))[1]},
    ) as sibling:
        rejected = await sibling.get("/admin/verify-pending", follow_redirects=False)
        assert rejected.status_code == 403

    async with AsyncClient(
        transport=transport,
        base_url=f"http://alpha.{BASE_DOMAIN}",
        cookies={security.ADMIN_COOKIE: security.set_session_cookie(operator_id)[1]},
    ) as operator:
        rejected = await operator.get("/admin/verify-pending", follow_redirects=False)
        assert rejected.status_code == 403

    async with AsyncClient(
        transport=transport,
        base_url=f"http://alpha.{BASE_DOMAIN}",
        cookies={security.ADMIN_COOKIE: security.set_session_cookie(inactive_owner_id)[1]},
    ) as inactive_owner:
        rejected = await inactive_owner.get(
            "/admin/verify-pending",
            follow_redirects=False,
        )
        assert rejected.status_code == 403

    async with AsyncClient(
        transport=transport,
        base_url=f"http://alpha.{BASE_DOMAIN}",
    ) as owner:
        await _login(owner, alpha)
        allowed = await owner.get("/admin/verify-pending")
        assert allowed.status_code == 200
        assert "Provider reconciliation" in allowed.text
        assert "cap=" not in allowed.text
        assert 'name="cap"' not in allowed.text
        assert allowed.headers["cache-control"] == "private, no-store"
        csrf = owner.cookies.get(security.CSRF_COOKIE)
        assert csrf

        missing_csrf = await owner.post(
            "/admin/verify-pending/reconcile",
            headers={"sec-fetch-site": "same-origin"},
            data={"outcome": "not-delivered"},
            follow_redirects=False,
        )
        reconciled = await owner.post(
            "/admin/verify-pending/reconcile",
            headers={"sec-fetch-site": "same-origin"},
            data={
                security.CSRF_FORM: csrf,
                "outcome": "not-delivered",
            },
            follow_redirects=False,
        )

    assert missing_csrf.status_code == 403
    assert reconciled.status_code == 303
    query = parse_qs(urlsplit(reconciled.headers["location"]).query)
    assert query == {"reconciled": ["not-delivered"]}
    assert (
        db.one(
            """SELECT status FROM signup_verification_intents
           WHERE studio_id='alpha'"""
        )["status"]
        == "failed"
    )
    assert (
        db.one(
            """SELECT status FROM signup_verification_intents
           WHERE studio_id='beta'"""
        )["status"]
        == "unknown"
    )
    assert (
        db.one(
            """SELECT COUNT(*) AS n FROM audit_log
           WHERE studio_id='alpha'
             AND action='signup.verification.reconciled.failed'"""
        )["n"]
        == 1
    )
    assert (
        db.one(
            """SELECT COUNT(*) AS n FROM audit_log
           WHERE studio_id='beta'
             AND action LIKE 'signup.verification.reconciled.%'"""
        )["n"]
        == 0
    )


@pytest.mark.asyncio
async def test_fresh_claim_is_rejected_but_stale_claim_can_be_reconciled(
    pending_http_env,
):
    account = _seed_unknown("claim-review")
    db.run(
        """UPDATE signup_verification_intents
           SET status='claimed', claim_token='fresh-claim-token',
               claimed_at=datetime('now'), error=NULL
           WHERE studio_id='claim-review'"""
    )
    transport = ASGITransport(app=pending_http_env)
    async with AsyncClient(
        transport=transport,
        base_url=f"http://claim-review.{BASE_DOMAIN}",
    ) as client:
        await _login(client, account)
        pending = await client.get("/admin/verify-pending")
        csrf = client.cookies.get(security.CSRF_COOKIE)
        assert csrf
        fresh = await client.post(
            "/admin/verify-pending/reconcile",
            headers={"sec-fetch-site": "same-origin"},
            data={
                security.CSRF_FORM: csrf,
                "outcome": "delivered",
            },
            follow_redirects=False,
        )
        db.run(
            """UPDATE signup_verification_intents
               SET claimed_at=datetime('now','-11 minutes')
               WHERE studio_id='claim-review'"""
        )
        stale = await client.post(
            "/admin/verify-pending/reconcile",
            headers={"sec-fetch-site": "same-origin"},
            data={
                security.CSRF_FORM: csrf,
                "outcome": "delivered",
            },
            follow_redirects=False,
        )

    assert fresh.status_code == 409
    assert stale.status_code == 303
    assert (
        db.one(
            """SELECT status, sent_at, claim_token FROM signup_verification_intents
           WHERE studio_id='claim-review'"""
        )["status"]
        == "sent"
    )
    assert (
        db.one(
            """SELECT claim_token FROM signup_verification_intents
           WHERE studio_id='claim-review'"""
        )["claim_token"]
        is None
    )
    studio = db.one(
        """SELECT provisioning_status, provisioning_error FROM studio
           WHERE id='claim-review'"""
    )
    assert dict(studio) == {"provisioning_status": "ready", "provisioning_error": None}
