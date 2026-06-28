"""Phase 15 — low priority + ops polish."""

import importlib

import eos.commerce as commerce
import eos.config as config
import eos.db as db
import eos.demo_sandbox as demo_sandbox
import eos.drive_time as drive_time
import eos.jobs as jobs
import eos.main as main
import eos.monitoring as monitoring
import eos.platform_admin as platform_admin
import eos.reports_export as reports_export
import eos.tenant as tenant
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_DEMO_ENABLED", "true")
    monkeypatch.setenv("EOS_PLATFORM_ADMIN_EMAILS", "admin@test.com")
    for mod in (
        config,
        db,
        jobs,
        tenant,
        demo_sandbox,
        drive_time,
        monitoring,
        platform_admin,
        commerce,
        reports_export,
        main,
    ):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    yield main.app
    jobs.stop()


def test_drive_time_haversine():
    mins = drive_time.travel_minutes(30.2672, -97.7431, 30.2849, -97.7341)
    assert 15 <= mins <= 60


def test_quickbooks_export_empty(app_env):
    tenant.set_studio("default")
    csv_body = reports_export.quickbooks_csv()
    assert "Date,Name,Memo,Amount" in csv_body


def test_ai_cull_job(app_env):
    tenant.set_studio("default")
    lid = db.run("INSERT INTO listings (studio_id, title) VALUES ('default', 'Cull')")
    gid = db.run(
        "INSERT INTO galleries (studio_id, listing_id, slug, title, pin, delivery_token) VALUES ('default', ?, 'cull-g', 'G', '1111', 'dt-cull')",
        (lid,),
    )
    sid = db.run("INSERT INTO sections (gallery_id, name, position) VALUES (?, 'Room', 0)", (gid,))
    for i in range(2):
        db.run(
            "INSERT INTO assets (gallery_id, section_id, kind, filename, stored, status) VALUES (?,?,?,?,?,?)",
            (gid, sid, "photo", f"p{i}.jpg", f"p{i}.jpg", "ready"),
        )
    jobs.enqueue("ai_cull", {"gallery_id": gid})
    jobs._execute(db.one("SELECT id FROM jobs ORDER BY id DESC")["id"])
    fav = db.one("SELECT COUNT(*) AS n FROM assets WHERE gallery_id=? AND agent_favorite=1", (gid,))
    assert fav["n"] == 1


@pytest.mark.asyncio
async def test_homeowner_booking_page(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/book/homeowner")
    assert r.status_code == 200
    assert "your home" in r.text.lower()


@pytest.mark.asyncio
async def test_demo_landing(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/demo")
    assert r.status_code == 200
    assert "demo" in r.text.lower()


def test_monitoring_health(app_env):
    details = monitoring.health_details()
    assert "disk_free_gb" in details
    assert details["version"] == "1.6.0"


def test_platform_admin_email_check(app_env):
    tenant.set_studio("default")
    uid = db.run(
        "INSERT INTO users (studio_id, email, password_hash, name, role) VALUES ('default', 'admin@test.com', 'x', 'A', 'owner')",
    )
    assert platform_admin.is_platform_admin(uid)
    assert not platform_admin.is_platform_admin(99999)
