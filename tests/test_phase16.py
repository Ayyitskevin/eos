"""Phase 16 — SaaS pivot: Connect, domain verify, platform ops, metering."""

import importlib
from unittest.mock import MagicMock, patch

import eos.config as config
import eos.db as db
import eos.domain_verify as domain_verify
import eos.jobs as jobs
import eos.main as main
import eos.onboarding as onboarding
import eos.plan_limits as plan_limits
import eos.stripe_connect as stripe_connect
import eos.tenant as tenant
import eos.usage as usage
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

    for mod in (
        config,
        db,
        jobs,
        tenant,
        onboarding,
        plan_limits,
        usage,
        users,
        stripe_connect,
        domain_verify,
        main,
    ):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    yield main.app
    jobs.stop()


def test_per_tenant_storage_isolation(saas_env, tmp_path):
    media = tmp_path / "data" / "media"
    (media / "alpha").mkdir(parents=True)
    (media / "alpha" / "f.jpg").write_bytes(b"x" * 1000)
    (media / "beta").mkdir(parents=True)
    (media / "beta" / "g.jpg").write_bytes(b"y" * 2000)

    assert usage.studio_storage_bytes(studio_id="alpha") == 1000
    assert usage.studio_storage_bytes(studio_id="beta") == 2000


def _seed_studio(slug: str, email: str, plan_tier: str = "trial") -> None:
    db.run(
        """INSERT INTO studio (id, name, slug, contact_email, saas_enabled, plan_tier, billing_status, active)
           VALUES (?,?,?,?,1,?, 'trialing', 1)""",
        (slug, slug.title(), slug, email, plan_tier),
    )
    users.create_user(email, "secret-pass-1", name="Owner", role="owner", studio_id=slug)


def test_team_seat_limit_enforced(saas_env):
    _seed_studio("seat-test", "owner@seat.test")
    tenant.set_studio("seat-test")
    with pytest.raises(Exception) as exc:
        users.create_user("second@seat.test", "secret-pass-2", role="operator")
    assert "Team seat" in str(exc.value.detail)


@patch("eos.domain_verify.httpx.get")
def test_domain_verify_txt(mock_get, saas_env):
    mock_get.return_value = MagicMock(
        raise_for_status=MagicMock(),
        json=MagicMock(return_value={"Answer": [{"data": '"eos-abc123"'}]}),
    )
    ok, msg = domain_verify.verify_domain(
        domain="photos.example.com",
        slug="gamma",
        token="eos-abc123",
    )
    assert ok
    assert "TXT" in msg


@patch("eos.domain_verify.httpx.get")
def test_domain_pending_until_verified(mock_get, saas_env):
    _seed_studio("domain-co", "d@domain.test", plan_tier="pro")
    tenant.set_studio("domain-co")
    token = domain_verify.save_pending_domain("photos.domain-co.com")
    row = db.one("SELECT custom_domain_verified FROM studio WHERE id=?", ("domain-co",))
    assert row["custom_domain_verified"] == 0

    mock_get.return_value = MagicMock(
        raise_for_status=MagicMock(),
        json=MagicMock(return_value={"Answer": [{"data": f'"{token}"'}]}),
    )
    ok, _ = domain_verify.try_verify_saved()
    assert ok
    row = db.one("SELECT custom_domain_verified FROM studio WHERE id=?", ("domain-co",))
    assert row["custom_domain_verified"] == 1


@patch("eos.stripe_connect.stripe.Account.create")
@patch("eos.stripe_connect.stripe.AccountLink.create")
def test_connect_onboarding_url(mock_link, mock_acct, saas_env, monkeypatch):
    monkeypatch.setenv("EOS_STRIPE_PLATFORM_SECRET_KEY", "sk_test_platform")
    importlib.reload(config)
    importlib.reload(stripe_connect)
    mock_acct.return_value = MagicMock(id="acct_test123")
    mock_link.return_value = MagicMock(url="https://connect.stripe.com/onboard")
    _seed_studio("pay-co", "p@pay.test")
    tenant.set_studio("pay-co")
    url = stripe_connect.onboarding_url(
        refresh_url="http://pay-co.eos.test/admin/stripe/connect",
        return_url="http://pay-co.eos.test/admin/stripe/connect?thanks=1",
    )
    assert url.startswith("https://connect.stripe.com")
    row = db.one("SELECT stripe_connect_account_id FROM studio WHERE id=?", ("pay-co",))
    assert row["stripe_connect_account_id"] == "acct_test123"


@pytest.mark.asyncio
async def test_saas_marketing_on_apex(saas_env):
    transport = ASGITransport(app=saas_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/", headers={"host": "eos.test"})
        assert r.status_code == 200
        assert "RE photography platform" in r.text
        assert "/signup" in r.text


@pytest.mark.asyncio
async def test_platform_suspend_blocks_subdomain(saas_env):
    _seed_studio("suspend-me", "s@suspend.test")
    db.run("UPDATE studio SET active=0 WHERE id=?", ("suspend-me",))
    transport = ASGITransport(app=saas_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/book", headers={"host": "suspend-me.eos.test"})
        assert r.status_code == 403
        assert "unavailable" in r.text
