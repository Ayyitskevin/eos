"""Phase 4 — questionnaires, booking, automations."""

import importlib
import json

import pytest
from httpx import ASGITransport, AsyncClient

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.main as main


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    for mod in (config, db, jobs, main):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    yield main.app
    jobs.stop()


@pytest.mark.asyncio
async def test_questionnaire_merges_and_advances(app_env):
    transport = ASGITransport(app=app_env)
    lid = db.run("INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Intake Test', 'booked')")
    from eos import questionnaires
    qid = questionnaires.create_for_listing(lid)
    token = db.one("SELECT token FROM questionnaires WHERE id=?", (qid,))["token"]

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            f"/q/{token}",
            data={"lockbox_code": "1234", "agent_on_site": "Yes", "special_requests": "Twilight hero"},
        )
        assert r.status_code == 303

    listing = db.one("SELECT status, access_notes FROM listings WHERE id=?", (lid,))
    assert listing["status"] == "shooting"
    assert "1234" in listing["access_notes"]
    q = db.one("SELECT status, answers FROM questionnaires WHERE id=?", (qid,))
    assert q["status"] == "completed"
    assert json.loads(q["answers"])["lockbox_code"] == "1234"


@pytest.mark.asyncio
async def test_book_inquiry(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            "/book",
            data={
                "name": "Agent Smith",
                "email": "agent@example.com",
                "property_address": "1 Main St",
                "message": "Need twilight",
            },
        )
        assert r.status_code == 200
        assert "Thanks" in r.text
    row = db.one("SELECT name FROM inquiries WHERE email=?", ("agent@example.com",))
    assert row["name"] == "Agent Smith"