"""Tenant isolation and auth hardening tests."""

import importlib

import pytest
from httpx import ASGITransport, AsyncClient

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.main as main
import eos.onboarding as onboarding
import eos.tenant as tenant


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("EOS_BASE_DOMAIN", "eos.test")
    monkeypatch.setenv("EOS_SAAS_MODE", "true")
    for mod in (config, db, jobs, tenant, onboarding, main):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    onboarding.create_studio(
        name="Alpha", slug="alpha",
        owner_email="a@alpha.test", owner_password="alpha-pass-1",
    )
    onboarding.create_studio(
        name="Beta", slug="beta",
        owner_email="b@beta.test", owner_password="beta-pass-1",
    )
    yield main.app
    jobs.stop()


@pytest.mark.asyncio
async def test_cross_tenant_admin_blocked(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "a@alpha.test", "password": "alpha-pass-1"},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        cookie = login.headers["set-cookie"]
        r = await client.get(
            "/admin",
            headers={"host": "beta.eos.test", "cookie": cookie},
        )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_legacy_admin_disabled_multi_studio(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            "/admin/login",
            data={"password": "test-admin-pass"},
            follow_redirects=False,
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_upload_rejects_other_studio_gallery(app_env):
    tenant.set_studio("alpha")
    gid = db.run(
        "INSERT INTO galleries (studio_id, slug, title, pin, delivery_token) VALUES ('alpha','s1','G','0000','tok')",
    )
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "b@beta.test", "password": "beta-pass-1"},
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        cookie = login.headers["set-cookie"]
        r = await client.post(
            f"/admin/galleries/{gid}/upload",
            files={"files": ("x.jpg", b"fake", "image/jpeg")},
            headers={"host": "beta.eos.test", "cookie": cookie},
        )
        assert r.status_code == 404