"""Phase 18 — per-tenant email, invite-only signup, onboarding."""

import importlib
from unittest.mock import MagicMock, patch

import eos.config as config
import eos.db as db
import eos.invites as invites_mod
import eos.jobs as jobs
import eos.mailer as mailer
import eos.main as main
import eos.onboarding as onboarding
import eos.tenant as tenant
import eos.users as users
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def saas_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_SAAS_MODE", "true")
    monkeypatch.setenv("EOS_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("EOS_BASE_DOMAIN", "eos.test")
    monkeypatch.setenv("EOS_EMAIL_PROVIDER", "postmark")
    monkeypatch.setenv("EOS_POSTMARK_API_KEY", "pm-test-key")
    monkeypatch.setenv("EOS_POSTMARK_FROM_EMAIL", "notify@test.com")
    for mod in (config, db, jobs, tenant, invites_mod, mailer, onboarding, users, main):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    yield main.app
    jobs.stop()


def _seed_invite(code: str = "BETA1", max_uses: int | None = 10) -> None:
    db.run(
        "INSERT INTO invite_codes (code, label, max_uses) VALUES (?,?,?)",
        (code, "test", max_uses),
    )


def test_invite_required_blocks_signup(saas_env, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("EOS_SIGNUP_INVITE_ONLY", "true")
    importlib.reload(config)
    importlib.reload(invites_mod)
    with pytest.raises(HTTPException):
        invites_mod.validate("")


@patch("eos.mailer.httpx.post")
def test_send_for_studio_postmark(mock_post, saas_env):
    mock_post.return_value = MagicMock(status_code=200, text="{}")
    db.run(
        """INSERT INTO studio (id, name, slug, contact_email, saas_enabled, plan_tier, active)
           VALUES ('mail-test','Mail Test','mail-test','owner@studio.test',1,'trial',1)""",
    )
    tenant.set_studio("mail-test")
    mailer.send_for_studio("agent@test.com", "Hello", "Body text")
    payload = mock_post.call_args.kwargs["json"]
    assert "Mail Test via" in payload["From"]
    assert payload["ReplyTo"] == "owner@studio.test"


@patch("eos.signup_verify.mailer.send_platform")
@pytest.mark.asyncio
async def test_signup_with_invite_code(mock_send, saas_env, monkeypatch):
    monkeypatch.setenv("EOS_SIGNUP_INVITE_ONLY", "true")
    importlib.reload(config)
    importlib.reload(invites_mod)
    _seed_invite("BETA2026")
    transport = ASGITransport(app=saas_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            "/signup",
            data={
                "studio_name": "Invite Studio",
                "slug": "invite-studio",
                "owner_email": "owner@invite.test",
                "owner_password": "secret-pass-1",
                "invite_code": "beta2026",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
    row = db.one("SELECT uses FROM invite_codes WHERE code='BETA2026'")
    assert row["uses"] == 1


@pytest.mark.asyncio
async def test_pricing_page_on_apex(saas_env):
    transport = ASGITransport(app=saas_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/pricing", headers={"host": "eos.test"})
        assert r.status_code == 200
        assert "Starter" in r.text