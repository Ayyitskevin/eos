"""Tenant isolation and auth hardening tests."""

import importlib

import eos.config as config
import eos.db as db
import eos.jobs as jobs
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
    monkeypatch.setenv("EOS_BASE_DOMAIN", "eos.test")
    monkeypatch.setenv("EOS_SAAS_MODE", "true")
    for mod in (config, db, jobs, security, tenant, onboarding, main):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    onboarding.create_studio(
        name="Alpha",
        slug="alpha",
        owner_email="a@alpha.test",
        owner_password="alpha-pass-1",
    )
    onboarding.create_studio(
        name="Beta",
        slug="beta",
        owner_email="b@beta.test",
        owner_password="beta-pass-1",
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


@pytest.mark.asyncio
async def test_admin_post_requires_csrf_with_browser_metadata(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "a@alpha.test", "password": "alpha-pass-1"},
            headers={"host": "alpha.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(security.CSRF_COOKIE)
        assert csrf

        blocked = await client.post(
            "/admin/clients",
            data={"name": "Blocked Agent"},
            headers={"host": "alpha.eos.test", "sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert blocked.status_code == 403

        allowed = await client.post(
            "/admin/clients",
            data={"name": "Allowed Agent", security.CSRF_FORM: csrf},
            headers={"host": "alpha.eos.test", "sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert allowed.status_code == 303


@pytest.mark.asyncio
async def test_cross_tenant_client_mutation_blocked(app_env):
    tenant.set_studio("alpha")
    alpha_client = db.run(
        "INSERT INTO clients (studio_id, name, email) VALUES ('alpha', 'Alpha Agent', 'agent@alpha.test')"
    )

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "b@beta.test", "password": "beta-pass-1"},
            headers={"host": "beta.eos.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get(security.CSRF_COOKIE)
        assert csrf
        headers = {"host": "beta.eos.test", "sec-fetch-site": "same-origin"}

        update = await client.post(
            f"/admin/clients/{alpha_client}",
            data={
                "name": "Tampered",
                "client_type": "agent",
                security.CSRF_FORM: csrf,
            },
            headers=headers,
            follow_redirects=False,
        )
        assert update.status_code == 404
        row = db.one("SELECT name FROM clients WHERE id=?", (alpha_client,))
        assert row["name"] == "Alpha Agent"

        create_child = await client.post(
            "/admin/clients",
            data={"name": "Bad Child", "parent_id": str(alpha_client), security.CSRF_FORM: csrf},
            headers=headers,
            follow_redirects=False,
        )
        assert create_child.status_code == 404
        leaked = db.one(
            "SELECT 1 AS x FROM clients WHERE studio_id='beta' AND parent_id=?",
            (alpha_client,),
        )
        assert leaked is None
