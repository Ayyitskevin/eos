"""Phase 13 — calendar, favorites, plan limits, API writes, upsell, custom domains."""

import importlib

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.tenant as tenant
import eos.plan_limits as plan_limits
import eos.usage as usage
import eos.api_tokens as api_tokens
import eos.listings as listings
import eos.galleries as galleries
import eos.calendar_view as calendar_view


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    for mod in (config, db, jobs, tenant, plan_limits, usage, api_tokens, listings, galleries, calendar_view):
        importlib.reload(mod)
    import eos.main as main
    importlib.reload(main)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    tenant.set_studio("default")
    yield main.app
    jobs.stop()


@pytest.mark.asyncio
async def test_calendar_month_view(env):
    transport = ASGITransport(app=env)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False)
        r = await client.get("/admin/calendar?view=month", cookies=login.cookies)
    assert r.status_code == 200
    assert "cal-month" in r.text


@pytest.mark.asyncio
async def test_api_create_listing(env):
    tenant.set_studio("default")
    _tid, raw = api_tokens.create_token(label="test")
    transport = ASGITransport(app=env)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/listings",
            headers={"authorization": f"Bearer {raw}"},
            json={"title": "API Listing", "address_line1": "1 Test St"},
        )
    assert r.status_code == 201
    assert r.json()["title"] == "API Listing"


@pytest.mark.asyncio
async def test_plan_limit_blocks_extra_tokens(env):
    tenant.set_studio("default")
    db.run("UPDATE studio SET plan_tier='trial' WHERE id='default'")
    for i in range(plan_limits.limits_for()["api_tokens"]):
        api_tokens.create_token(label=f"t{i}")
    with pytest.raises(HTTPException):
        api_tokens.create_token(label="overflow")


def test_agent_favorite_toggle(env):
    tenant.set_studio("default")
    lid = listings.create_listing("Fav test")
    gid = galleries.create_gallery("G", listing_id=lid)
    aid = db.run(
        """INSERT INTO assets (gallery_id, filename, stored, status, kind)
           VALUES (?,?,?,?,?)""",
        (gid, "a.jpg", "a.jpg", "ready", "photo"),
    )
    assert galleries.toggle_agent_favorite(aid, gallery_id=gid) is True
    assert len(galleries.agent_favorites(gid)) == 1


def test_revision_round(env):
    tenant.set_studio("default")
    lid = listings.create_listing("Rev test")
    listings.update_listing(lid, status="delivered")
    listings.request_revision(lid, notes="Fix sky")
    row = listings.get_listing(lid)
    assert row["revision_round"] == 1
    assert row["status"] == "editing"


def test_custom_domain_resolution(env):
    tenant.set_studio("default")
    db.run(
        "UPDATE studio SET custom_domain='photos.example.com', custom_domain_verified=1 WHERE id='default'",
    )
    sid = tenant.studio_id_for_custom_domain("photos.example.com")
    assert sid == "default"


def test_usage_bump(env):
    tenant.set_studio("default")
    usage.bump("api_calls", 3)
    snap = usage.snapshot()
    assert snap["api_calls"] >= 3


@pytest.mark.asyncio
async def test_inbound_api_event(env):
    tenant.set_studio("default")
    lid = listings.create_listing("Inbound")
    _tid, raw = api_tokens.create_token()
    transport = ASGITransport(app=env)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/inbound/listing.delivered",
            headers={"authorization": f"Bearer {raw}"},
            json={"listing_id": lid},
        )
    assert r.status_code == 200
    assert listings.get_listing(lid)["status"] == "delivered"