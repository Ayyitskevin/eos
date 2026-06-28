"""Phase 11 — Google Calendar, Dropbox ingest, platform billing."""

import importlib

import pytest

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.oauth_store as oauth_store
import eos.platform_billing as platform_billing
import eos.secret_store as secret_store
import eos.tenant as tenant


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_GOOGLE_CLIENT_ID", "gclient")
    monkeypatch.setenv("EOS_GOOGLE_CLIENT_SECRET", "gsecret")
    monkeypatch.setenv("EOS_GOOGLE_REDIRECT_URI", "http://test/oauth/google/callback")
    monkeypatch.setenv("EOS_DROPBOX_APP_KEY", "dkey")
    monkeypatch.setenv("EOS_DROPBOX_APP_SECRET", "dsecret")
    monkeypatch.setenv("EOS_DROPBOX_REDIRECT_URI", "http://test/oauth/dropbox/callback")
    monkeypatch.setenv("EOS_STRIPE_PLATFORM_SECRET_KEY", "sk_test_platform")
    monkeypatch.setenv("EOS_STRIPE_PRICE_STARTER", "price_starter")
    monkeypatch.setenv("EOS_STRIPE_PRICE_PRO", "price_pro")
    for mod in (config, db, jobs, tenant, secret_store, oauth_store, platform_billing):
        importlib.reload(mod)
    import eos.integrations.dropbox as dropbox
    import eos.integrations.google_calendar as google_calendar
    importlib.reload(google_calendar)
    importlib.reload(dropbox)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    tenant.set_studio("default")
    yield
    jobs.stop()


def test_secret_store_roundtrip(env):
    assert secret_store.decrypt(secret_store.encrypt("tok_abc")) == "tok_abc"


def test_dropbox_resolve_listing_from_subfolder(env):
    from eos.integrations import dropbox
    import eos.studio as studio
    studio.get_profile()
    db.run(
        "UPDATE studio_profiles SET dropbox_watch_path='/Eos Ingest' WHERE studio_id='default'",
    )
    lid = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', '123 Main', 'booked')",
    )
    assert dropbox.resolve_listing_id(f"/Eos Ingest/{lid}/photo.jpg") == lid
    assert dropbox.resolve_listing_id("/Eos Ingest/photo.jpg") is None


def test_dropbox_default_listing(env):
    from eos.integrations import dropbox
    import eos.studio as studio
    studio.get_profile()
    lid = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Default', 'booked')",
    )
    db.run(
        "UPDATE studio_profiles SET dropbox_watch_path='/Ingest', dropbox_default_listing_id=? WHERE studio_id='default'",
        (lid,),
    )
    assert dropbox.resolve_listing_id("/Ingest/IMG_0001.jpg") == lid


def test_platform_billing_apply_subscription(env):
    platform_billing.apply_subscription(
        studio_id="default",
        subscription_id="sub_123",
        status="active",
        plan_tier="pro",
    )
    row = db.one("SELECT billing_status, plan_tier, stripe_subscription_id FROM studio WHERE id='default'")
    assert row["billing_status"] == "active"
    assert row["plan_tier"] == "pro"
    assert row["stripe_subscription_id"] == "sub_123"


def test_google_push_enqueues_job(env):
    import eos.integrations.google_calendar as google_calendar
    importlib.reload(google_calendar)
    import eos.studio as studio
    studio.get_profile()
    oauth_store.save_tokens("google", access_token="at", refresh_token="rt")
    db.run("UPDATE studio_profiles SET google_calendar_enabled=1 WHERE studio_id='default'")
    assert google_calendar.is_enabled()
    appt_id = db.run(
        """INSERT INTO appointments (studio_id, title, kind, starts_at, token)
           VALUES ('default','Shoot','shoot','2026-07-01 10:00:00','tok')""",
    )
    google_calendar.enqueue_push(appt_id)
    job = db.one("SELECT kind, payload FROM jobs ORDER BY id DESC LIMIT 1")
    assert job["kind"] == "google_calendar_push"
    assert str(appt_id) in job["payload"]


def test_integration_tables_exist(env):
    assert db.one("SELECT name FROM sqlite_master WHERE name='studio_oauth'")
    assert db.one("SELECT name FROM sqlite_master WHERE name='dropbox_ingest_log'")
    cols = {r[1] for r in db.connect().execute("PRAGMA table_info(studio)").fetchall()}
    assert "stripe_subscription_id" in cols
    assert "billing_status" in cols


def test_google_and_dropbox_configured(env):
    from eos.integrations import dropbox, google_calendar
    assert google_calendar.is_configured()
    assert dropbox.is_configured()
    assert platform_billing.is_configured()