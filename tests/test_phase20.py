"""Phase 20 — platform legal pages + in-process rate limiting."""

import importlib

import eos.api_tokens as api_tokens
import eos.config as config
import eos.db as db
import eos.main as main
import eos.onboarding as onboarding
import eos.security as security
import eos.tenant as tenant
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("EOS_SIGNUP_AUTO_VERIFY_LOCAL", "true")
    monkeypatch.setenv("EOS_BASE_DOMAIN", "eos.test")
    monkeypatch.setenv("EOS_SAAS_MODE", "true")
    for mod in (config, api_tokens, db, security, tenant, onboarding, main):
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
async def test_legal_pages_render_on_apex_and_tenant_host(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://eos.test") as client:
        for host in ("eos.test", "alpha.eos.test"):
            terms = await client.get("/terms", headers={"host": host})
            assert terms.status_code == 200
            assert "Terms of Service" in terms.text
            privacy = await client.get("/privacy", headers={"host": host})
            assert privacy.status_code == 200
            assert "Privacy Policy" in privacy.text


@pytest.mark.asyncio
async def test_marketing_and_signup_link_legal_pages(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://eos.test") as client:
        home = await client.get("/", headers={"host": "eos.test"})
        assert home.status_code == 200
        assert 'href="/terms"' in home.text
        assert 'href="/privacy"' in home.text
        signup = await client.get("/signup", headers={"host": "eos.test"})
        assert signup.status_code == 200
        assert 'href="/terms"' in signup.text
        assert 'href="/privacy"' in signup.text


@pytest.mark.asyncio
async def test_api_bearer_token_rate_limited(app_env, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_API_PER_MIN", 3)
    tenant.set_studio("alpha")
    _token_id, raw_token = api_tokens.create_token(label="rate-limit")
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://eos.test") as client:
        headers = {"host": "alpha.eos.test", "authorization": f"Bearer {raw_token}"}
        for _ in range(3):
            r = await client.get("/api/v1/listings", headers=headers)
            assert r.status_code == 200
        limited = await client.get("/api/v1/listings", headers=headers)
        assert limited.status_code == 429
        assert limited.headers["retry-after"]
        assert limited.json()["detail"] == "API rate limit exceeded"


@pytest.mark.asyncio
async def test_public_endpoint_rate_limited(app_env, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PUBLIC_PER_MIN", 2)
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://eos.test") as client:
        headers = {"host": "alpha.eos.test"}
        for _ in range(2):
            r = await client.get("/r/SUMMER26", headers=headers)
            assert r.status_code == 303
        limited = await client.get("/r/SUMMER26", headers=headers)
        assert limited.status_code == 429
        assert limited.headers["retry-after"]


@pytest.mark.asyncio
async def test_health_endpoints_exempt_from_rate_limits(app_env, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_API_PER_MIN", 1)
    monkeypatch.setattr(config, "RATE_LIMIT_PUBLIC_PER_MIN", 1)
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://eos.test") as client:
        for _ in range(3):
            r = await client.get("/healthz", headers={"host": "eos.test"})
            assert r.status_code == 200
            ready = await client.get("/readyz", headers={"host": "eos.test"})
            assert ready.status_code == 200


@pytest.mark.asyncio
async def test_disabled_rate_limits_allow_normal_traffic(app_env, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_API_PER_MIN", 0)
    monkeypatch.setattr(config, "RATE_LIMIT_PUBLIC_PER_MIN", 0)
    tenant.set_studio("alpha")
    _token_id, raw_token = api_tokens.create_token(label="no-limit")
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://eos.test") as client:
        for _ in range(5):
            r = await client.get("/r/SUMMER26", headers={"host": "alpha.eos.test"})
            assert r.status_code == 303
            api = await client.get(
                "/api/v1/listings",
                headers={"host": "alpha.eos.test", "authorization": f"Bearer {raw_token}"},
            )
            assert api.status_code == 200
