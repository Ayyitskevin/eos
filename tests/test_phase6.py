"""Phase 6 — smart booking, packages, deposits."""

import importlib

import pytest
from httpx import ASGITransport, AsyncClient

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.main as main
import eos.scheduling as scheduling
import eos.studio as studio


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    for mod in (config, db, jobs, scheduling, main):
        importlib.reload(mod)
    import eos.studio as studio_mod
    importlib.reload(studio_mod)
    config.ensure_dirs()
    db.migrate()
    studio_mod.update_profile(booking_enabled=True, min_notice_hours=0, buffer_minutes=0)
    jobs.start()
    yield main.app
    jobs.stop()


def _first_slot():
    slots = scheduling.open_slots(days=7)
    assert slots, "expected at least one open slot"
    return slots[0]["value"]


@pytest.mark.asyncio
async def test_open_slots_respect_appointments(app_env):
    slot = _first_slot()
    from eos import appointments
    appointments.create_appointment("Busy", starts_at=slot)
    db.run("UPDATE appointments SET status='confirmed' WHERE starts_at=?", (slot,))
    assert not scheduling.slot_is_open(slot)
    assert scheduling.slot_is_open(_first_slot())


@pytest.mark.asyncio
async def test_booking_creates_listing_and_appointment(app_env):
    pkg = db.one("SELECT id FROM service_packages WHERE active=1 LIMIT 1")
    db.run("UPDATE service_packages SET deposit_cents=0 WHERE id=?", (pkg["id"],))
    slot = _first_slot()
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            "/book",
            data={
                "name": "Agent Lee",
                "email": "agent@book.test",
                "phone": "555-0100",
                "property_address": "99 Maple Ave",
                "package_id": pkg["id"],
                "scheduled_at": slot,
                "signer_name": "Agent Lee",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "/booking/" in r.headers["location"]

        inq = db.one("SELECT * FROM inquiries WHERE email=?", ("agent@book.test",))
        assert inq["status"] == "confirmed"
        assert inq["listing_id"]
        listing = db.one("SELECT status FROM listings WHERE id=?", (inq["listing_id"],))
        assert listing["status"] == "booked"
        appt = db.one("SELECT status FROM appointments WHERE id=?", (inq["appointment_id"],))
        assert appt["status"] == "confirmed"


@pytest.mark.asyncio
async def test_booking_deposit_pending_payment(app_env):
    pkg = db.one("SELECT id FROM service_packages WHERE name='Standard Listing'")
    db.run("UPDATE service_packages SET deposit_cents=5000 WHERE id=?", (pkg["id"],))
    slot = _first_slot()
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            "/book",
            data={
                "name": "Deposit Agent",
                "email": "deposit@book.test",
                "property_address": "1 Pine Rd",
                "package_id": pkg["id"],
                "scheduled_at": slot,
                "signer_name": "Deposit Agent",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        inq = db.one("SELECT status, listing_id, invoice_id FROM inquiries WHERE email=?", ("deposit@book.test",))
        assert inq["status"] == "pending_payment"
        listing = db.one("SELECT status FROM listings WHERE id=?", (inq["listing_id"],))
        assert listing["status"] == "lead"
        inv = db.one("SELECT invoice_kind, amount_cents FROM invoices WHERE id=?", (inq["invoice_id"],))
        assert inv["invoice_kind"] == "deposit"
        assert inv["amount_cents"] == 5000