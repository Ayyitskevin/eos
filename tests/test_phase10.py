"""Phase 10 — multi-tenant SaaS platform."""

import importlib

import eos.api_tokens as api_tokens
import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.main as main
import eos.onboarding as onboarding
import eos.referrals as referrals
import eos.sequences as sequences
import eos.tenant as tenant
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("EOS_BASE_DOMAIN", "eos.test")
    for mod in (config, db, jobs, tenant, onboarding, api_tokens, referrals, sequences, main):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    yield main.app
    jobs.stop()


@pytest.mark.asyncio
async def test_signup_creates_isolated_studio(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            "/signup",
            data={
                "studio_name": "Beta Studio",
                "slug": "beta-studio",
                "owner_email": "owner@beta.test",
                "owner_password": "secret-pass-1",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "beta-studio.eos.test" in r.headers["location"]

    tenant.set_studio("beta-studio")
    pkgs = db.all_("SELECT name FROM service_packages WHERE studio_id=?", ("beta-studio",))
    assert len(pkgs) >= 3
    seqs = db.all_("SELECT slug FROM email_sequences WHERE studio_id=?", ("beta-studio",))
    assert any(s["slug"] == "booking-confirm" for s in seqs)


@pytest.mark.asyncio
async def test_subdomain_resolves_tenant(app_env):
    onboarding.create_studio(
        name="Gamma",
        slug="gamma",
        owner_email="g@gamma.test",
        owner_password="secret-pass-2",
    )
    db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('gamma', 'Gamma Only', 'lead')",
    )
    db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Default Only', 'lead')",
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "g@gamma.test", "password": "secret-pass-2"},
            headers={"host": "gamma.eos.test"},
            follow_redirects=False,
        )
        cookie = login.headers["set-cookie"]
        r = await client.get("/admin", headers={"host": "gamma.eos.test", "cookie": cookie})
        assert r.status_code == 200
        assert "Gamma Only" in r.text
        assert "Default Only" not in r.text


@pytest.mark.asyncio
async def test_api_token_lists_tenant_listings(app_env):
    tenant.set_studio("default")
    db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', 'API Listing', 'lead')"
    )
    _tid, raw = api_tokens.create_token(label="test")

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/api/v1/listings", headers={"authorization": f"Bearer {raw}"})
        assert r.status_code == 200
        data = r.json()
        items = data["items"] if isinstance(data, dict) else data
        assert any(row["title"] == "API Listing" for row in items)


def test_sequence_update(app_env):
    tenant.set_studio("default")
    seq = db.one("SELECT id FROM email_sequences WHERE studio_id='default' LIMIT 1")
    sequences.update_sequence(
        seq["id"],
        name="Updated name",
        subject="Hi {client_first}",
        body_template="Body {site_name}",
        delay_hours=2,
        trigger_event="listing.booked",
    )
    row = sequences.get_sequence(seq["id"])
    assert row["name"] == "Updated name"
    assert row["delay_hours"] == 2


def test_referral_credit(app_env):
    tenant.set_studio("default")
    referrals.create_code(code="REF25", credit_cents=2500)
    total, ref = referrals.apply_credit("REF25", 17500)
    assert total == 15000
    assert ref is not None
