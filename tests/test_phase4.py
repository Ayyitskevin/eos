"""Phase 4 — questionnaires, booking, automations."""

import importlib
import json

import eos.db as db
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_questionnaire_merges_and_advances(app_env):
    transport = ASGITransport(app=app_env)
    lid = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', 'Intake Test', 'booked')"
    )
    from eos import questionnaires

    qid = questionnaires.create_for_listing(lid)
    token = db.one("SELECT token FROM questionnaires WHERE id=?", (qid,))["token"]

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            f"/q/{token}",
            data={
                "lockbox_code": "1234",
                "agent_on_site": "Yes",
                "special_requests": "Twilight hero",
            },
        )
        assert r.status_code == 303

    listing = db.one("SELECT status, access_notes FROM listings WHERE id=?", (lid,))
    assert listing["status"] == "shooting"
    assert "1234" in listing["access_notes"]
    q = db.one("SELECT status, answers FROM questionnaires WHERE id=?", (qid,))
    assert q["status"] == "completed"
    assert json.loads(q["answers"])["lockbox_code"] == "1234"


@pytest.mark.asyncio
async def test_book_page_shows_packages(app_env):
    import eos.scheduling as scheduling
    import eos.studio as studio

    importlib.reload(studio)
    studio.update_profile(booking_enabled=True, min_notice_hours=0)
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/book")
        assert r.status_code == 200
        assert "Standard Listing" in r.text
        assert scheduling.open_slots()
