"""Tenant-owned background job and operator-recovery integrity."""

import importlib
import json

import eos.commerce as commerce
import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.main as main
import eos.security as security
import eos.tenant as tenant
import eos.webhooks as webhooks
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_DEMO_ENABLED", "false")
    monkeypatch.setenv("EOS_SAAS_MODE", "false")
    monkeypatch.setenv("EOS_SIGNUP_ENABLED", "false")
    monkeypatch.setenv("EOS_BILLING_ENFORCE", "false")
    monkeypatch.setenv("EOS_BASE_URL", "http://testserver")
    for module in (config, db, jobs, tenant, commerce, security, main):
        importlib.reload(module)
    config.ensure_dirs()
    db.migrate()
    return main.app


def _seed_studio(studio_id: str) -> None:
    db.run(
        "INSERT INTO studio (id, name, slug) VALUES (?,?,?)",
        (studio_id, studio_id.title(), studio_id),
    )
    db.run("INSERT INTO studio_profiles (studio_id) VALUES (?)", (studio_id,))


def _user_cookie(studio_id: str = "default") -> str:
    uid = db.run(
        """INSERT INTO users (studio_id, email, password_hash, name, role)
           VALUES (?,?,?,?,?)""",
        (studio_id, f"owner@{studio_id}.test", "unused", "Owner", "owner"),
    )
    name, value = security.set_session_cookie(uid)
    return f"{name}={value}"


def test_enqueue_scopes_deduplication_and_worker_context(app_env, monkeypatch):
    _seed_studio("beta")

    tenant.set_studio("default")
    default_id = jobs.enqueue("integration_sweep", {}, idempotency_key="same-key")
    assert jobs.enqueue("integration_sweep", {}, idempotency_key="same-key") == default_id

    tenant.set_studio("beta")
    beta_id = jobs.enqueue("integration_sweep", {}, idempotency_key="same-key")
    assert beta_id != default_id
    assert db.one("SELECT studio_id FROM jobs WHERE id=?", (beta_id,))["studio_id"] == "beta"
    with pytest.raises(ValueError, match="active studio"):
        jobs.enqueue("dropbox_scan", {"studio_id": "default"})

    seen: list[str] = []
    monkeypatch.setitem(
        jobs.HANDLERS,
        "tenant_probe",
        lambda _payload: seen.append(tenant.get_studio_id()),
    )
    probe_id = jobs.enqueue("tenant_probe", {})
    tenant.set_studio("default")
    jobs._execute(probe_id)

    assert seen == ["beta"]
    assert tenant.get_studio_id() == "default"
    assert db.one("SELECT status FROM jobs WHERE id=?", (probe_id,))["status"] == "done"


def test_failed_job_listing_backfills_legacy_owner_and_retries_in_tenant(app_env, monkeypatch):
    _seed_studio("beta")
    listing_id = db.run("INSERT INTO listings (studio_id, title) VALUES ('beta', 'Legacy listing')")
    legacy_id = db.run(
        """INSERT INTO jobs (kind, payload, status, attempts, error)
           VALUES (?,?, 'failed', 3, 'legacy failure')""",
        ("geocode_listing", json.dumps({"listing_id": listing_id})),
    )
    default_id = db.run(
        """INSERT INTO jobs (studio_id, kind, payload, status, attempts, error)
           VALUES ('default', 'tenant_probe', '{}',
                   'failed', 3, 'default only')"""
    )

    tenant.set_studio("beta")
    failed = jobs.list_failed()
    assert [row["id"] for row in failed] == [legacy_id]
    assert db.one("SELECT studio_id FROM jobs WHERE id=?", (legacy_id,))["studio_id"] == "beta"

    tenant.set_studio("default")
    assert not jobs.retry_job(legacy_id)
    assert db.one("SELECT status FROM jobs WHERE id=?", (legacy_id,))["status"] == "failed"

    submitted: list[int] = []
    monkeypatch.setattr(jobs, "_submit", submitted.append)
    tenant.set_studio("beta")
    assert jobs.retry_job(legacy_id)
    row = db.one("SELECT status, attempts, error FROM jobs WHERE id=?", (legacy_id,))
    assert dict(row) == {"status": "queued", "attempts": 0, "error": None}
    assert submitted == [legacy_id]
    assert db.one(
        """SELECT 1 FROM audit_log
           WHERE studio_id='beta' AND action='job.retry' AND detail=?""",
        (f"job={legacy_id}",),
    )
    assert default_id not in [row["id"] for row in jobs.list_failed()]


@pytest.mark.asyncio
async def test_admin_recovery_is_tenant_scoped_and_expires_only_elapsed_holds(app_env, monkeypatch):
    _seed_studio("beta")
    cookie = _user_cookie()
    own_job = db.run(
        """INSERT INTO jobs (studio_id, kind, payload, status, attempts, error)
           VALUES ('default', 'export_crops', '{}',
                   'failed', 3, 'visible failure')"""
    )
    other_job = db.run(
        """INSERT INTO jobs (studio_id, kind, payload, status, attempts, error)
           VALUES ('beta', 'export_crops', '{}',
                   'failed', 3, 'hidden failure')"""
    )
    expired_id = db.run(
        """INSERT INTO inquiries
           (studio_id, name, email, property_address, status, scheduled_at, payment_expires_at)
           VALUES ('default', 'Expired Client', 'expired@test.invalid',
                   '1 Old St', 'pending_payment', datetime('now','+1 day'),
                   datetime('now','-16 minute'))"""
    )
    future_id = db.run(
        """INSERT INTO inquiries
           (studio_id, name, email, property_address, status, scheduled_at, payment_expires_at)
           VALUES ('default', 'Future Client', 'future@test.invalid',
                   '2 New St', 'pending_payment', datetime('now','+2 day'),
                   datetime('now','+30 minute'))"""
    )
    submitted: list[int] = []
    monkeypatch.setattr(jobs, "_submit", submitted.append)

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get("/admin/studio", headers={"cookie": cookie})
        assert page.status_code == 200
        assert "Operator recovery" in page.text
        assert "visible failure" in page.text
        assert "hidden failure" not in page.text
        assert "Expired Client" in page.text
        assert "Future Client" in page.text

        blocked = await client.post(
            f"/admin/studio/jobs/{other_job}/retry",
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        assert blocked.status_code == 404
        retried = await client.post(
            f"/admin/studio/jobs/{own_job}/retry",
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        assert retried.status_code == 303

        released = await client.post(
            "/admin/studio/pending-bookings/expire",
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        assert released.status_code == 303
        assert "expired_holds=1" in released.headers["location"]

    assert submitted == [own_job]
    assert db.one("SELECT status FROM jobs WHERE id=?", (other_job,))["status"] == "failed"
    assert db.one("SELECT status FROM inquiries WHERE id=?", (expired_id,))["status"] == "canceled"
    assert (
        db.one("SELECT status FROM inquiries WHERE id=?", (future_id,))["status"]
        == "pending_payment"
    )


@pytest.mark.asyncio
async def test_one_time_secrets_never_enter_redirect_urls_and_numeric_inputs_fail_closed(
    app_env, monkeypatch
):
    cookie = _user_cookie()

    def fake_webhook_create(*, label: str, url: str, events: list[str]) -> int:
        return db.run(
            """INSERT INTO webhook_subscriptions
               (studio_id, label, url, secret, events) VALUES (?,?,?,?,?)""",
            (tenant.get_studio_id(), label, url, "one-time-hook-secret", json.dumps(events)),
        )

    monkeypatch.setattr(webhooks, "create_subscription", fake_webhook_create)
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token_page = await client.post(
            "/admin/studio/api-tokens",
            data={"label": "One time"},
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        assert token_page.status_code == 200
        assert "location" not in token_page.headers
        assert "no-store" in token_page.headers["cache-control"]
        assert "New API token (copy now" in token_page.text
        assert "token=" not in str(token_page.url)

        clean_page = await client.get("/admin/studio", headers={"cookie": cookie})
        assert "New API token (copy now" not in clean_page.text

        hook_page = await client.post(
            "/admin/studio/webhooks",
            data={"label": "Test", "url": "https://example.invalid/hook"},
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        assert hook_page.status_code == 200
        assert "location" not in hook_page.headers
        assert "no-store" in hook_page.headers["cache-control"]
        assert "one-time-hook-secret" in hook_page.text
        assert "one-time-hook-secret" not in str(hook_page.url)
        clean_page = await client.get("/admin/studio", headers={"cookie": cookie})
        assert "one-time-hook-secret" not in clean_page.text

        package = db.one("SELECT id, name FROM service_packages WHERE studio_id='default' LIMIT 1")
        bad_package = await client.post(
            f"/admin/studio/packages/{package['id']}",
            data={
                "name": package["name"],
                "price_dollars": "100",
                "deposit_dollars": "101",
                "turnaround_hours": "24",
            },
            headers={"cookie": cookie},
        )
        assert bad_package.status_code == 400
        bad_referral = await client.post(
            "/admin/studio/referrals",
            data={"code": "BAD", "credit_dollars": "-1"},
            headers={"cookie": cookie},
        )
        assert bad_referral.status_code == 400


def test_migration_backfills_pending_payment_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "upgrade-data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    importlib.reload(config)
    importlib.reload(db)
    config.ensure_dirs()

    con = db.connect()
    try:
        db._ensure_migrations_table(con)
        migrations = db._discover_migrations()
        for version, name, path in migrations:
            if int(version) >= 18:
                break
            statements, disables_fk = db._prepare_migration(path.read_text())
            db._apply_migration(
                con,
                version,
                name,
                statements,
                disables_foreign_keys=disables_fk,
            )
        con.execute(
            """INSERT INTO inquiries
               (studio_id, name, email, status, created_at)
               VALUES ('default', 'Upgrade Hold', 'hold@test.invalid',
                       'pending_payment', '2026-07-26 12:00:00')"""
        )
        con.commit()
        version, name, path = next(item for item in migrations if int(item[0]) == 18)
        statements, disables_fk = db._prepare_migration(path.read_text())
        db._apply_migration(
            con,
            version,
            name,
            statements,
            disables_foreign_keys=disables_fk,
        )
        row = con.execute(
            "SELECT payment_expires_at FROM inquiries WHERE name='Upgrade Hold'"
        ).fetchone()
        assert row["payment_expires_at"] == "2026-07-26 13:00:00"
    finally:
        con.close()
