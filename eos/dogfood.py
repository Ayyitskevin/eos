"""Seed one realistic listing through the full Eos pipeline for local dogfooding."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

from PIL import Image

from . import (
    appointments,
    clients,
    config,
    credits,
    db,
    galleries,
    invoices,
    listings,
    media_paths,
    microsites,
    scheduling,
    studio,
    studio_seed,
    tenant,
    users,
)

log = logging.getLogger("eos.dogfood")

MARKER = "dogfood:v1"
ADDRESS = "1420 Maple Dr"
SITE_SLUG = "1420-maple-dr"
GALLERY_PIN = "2847"

_PHOTOS = [
    ("front-elevation.jpg", "Exterior & Curb Appeal", (62, 110, 78), True),
    ("living-room.jpg", "Living Areas", (88, 72, 58), False),
    ("kitchen.jpg", "Kitchen", (140, 120, 95), False),
    ("primary-bedroom.jpg", "Bedrooms", (75, 95, 130), False),
    ("primary-bath.jpg", "Bathrooms", (110, 130, 145), False),
    ("detail-fireplace.jpg", "Details & Amenities", (55, 55, 55), False),
]


def _existing() -> dict | None:
    row = db.one(
        """SELECT id FROM listings
           WHERE studio_id='default' AND (notes=? OR address_line1=?)""",
        (MARKER, ADDRESS),
    )
    if not row:
        return None
    return _summary(row["id"])


def _shoot_times() -> tuple[str, str, str]:
    shoot_day = dt.date.today() - dt.timedelta(days=5)
    starts = dt.datetime.combine(shoot_day, dt.time(10, 0)).strftime("%Y-%m-%d %H:%M:%S")
    ends = scheduling.ends_at_for(starts)
    shoot_date = shoot_day.isoformat()
    return starts, ends, shoot_date


def _seed_photos(gallery_id: int) -> int:
    sections = {s["name"]: s["id"] for s in galleries.gallery_sections(gallery_id)}
    cover_id = None
    for filename, section_name, color, favorite in _PHOTOS:
        section_id = sections.get(section_name)
        stored = filename
        img = Image.new("RGB", (1600, 1067), color=color)
        for sub in ("original", "web", "thumb"):
            dest = media_paths.gallery_subdir(gallery_id, sub) / stored
            size = (
                (1600, 1067) if sub == "original" else ((1200, 800) if sub == "web" else (480, 320))
            )
            img.resize(size, Image.Resampling.LANCZOS).save(dest, "JPEG", quality=88)
        asset_id = db.run(
            """INSERT INTO assets
               (gallery_id, section_id, kind, filename, stored, bytes, status, width, height, agent_favorite)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                gallery_id,
                section_id,
                "photo",
                filename,
                stored,
                250_000,
                "ready",
                1600,
                1067,
                1 if favorite else 0,
            ),
        )
        if favorite:
            cover_id = asset_id
    if cover_id:
        db.run("UPDATE galleries SET cover_asset_id=? WHERE id=?", (cover_id, gallery_id))
    return cover_id or 0


def seed(*, force: bool = False) -> dict:
    tenant.set_studio("default")
    db.migrate()

    existing = _existing()
    if existing and not force:
        log.info("dogfood listing already exists (id=%s)", existing["listing_id"])
        return existing

    if existing and force:
        lid = existing["listing_id"]
        for gid_row in db.all_("SELECT id FROM galleries WHERE listing_id=?", (lid,)):
            gid = gid_row["id"]
            db.run("DELETE FROM assets WHERE gallery_id=?", (gid,))
            db.run("DELETE FROM sections WHERE gallery_id=?", (gid,))
            db.run("DELETE FROM galleries WHERE id=?", (gid,))
        db.run("DELETE FROM appointments WHERE listing_id=?", (lid,))
        db.run("DELETE FROM invoices WHERE listing_id=?", (lid,))
        db.run("DELETE FROM listing_shots WHERE listing_id=?", (lid,))
        db.run("DELETE FROM listing_tasks WHERE listing_id=?", (lid,))
        db.run("DELETE FROM listings WHERE id=?", (lid,))
        agent = db.one(
            "SELECT id FROM clients WHERE studio_id='default' AND email='sarah.chen@kw.com'"
        )
        if agent:
            db.run("DELETE FROM credit_ledger WHERE client_id=?", (agent["id"],))
            db.run("DELETE FROM clients WHERE id=?", (agent["id"],))
        db.run("DELETE FROM clients WHERE studio_id='default' AND email='billing@maplebroker.com'")

    if db.one("SELECT COUNT(*) AS n FROM service_packages WHERE studio_id='default'")["n"] == 0:
        studio_seed.seed_studio("default")

    profile = studio.get_profile()
    if not profile["published"]:
        studio.update_profile(published=True, booking_enabled=True)

    email = config.BOOTSTRAP_EMAIL.strip().lower() or "owner@localhost"
    password = config.ADMIN_PASSWORD or "dogfood-admin"
    if not users.get_by_email(email):
        users.create_user(email, password, name="Studio Owner", role="owner")
        log.info("created owner %s", email)

    broker_id = clients.create_client(
        "Maple Street Realty",
        client_type="brokerage",
        company="Maple Street Realty",
        email="billing@maplebroker.com",
        phone="(512) 555-0100",
    )
    db.run("UPDATE clients SET portal_token=? WHERE id=?", ("df-broker-maple", broker_id))

    agent_id = clients.create_client(
        "Sarah Chen",
        client_type="agent",
        company="Maple Street Realty · Keller Williams",
        email="sarah.chen@kw.com",
        phone="(512) 555-0142",
        license_number="TX-8844221",
        parent_id=broker_id,
    )
    db.run("UPDATE clients SET portal_token=? WHERE id=?", ("df-agent-sarah", agent_id))
    credits.add_credit(agent_id, amount_cents=5000, note="Welcome referral credit")

    starts_at, ends_at, shoot_date = _shoot_times()
    due_at = (dt.date.today() + dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M")

    listing_id = listings.create_listing(
        "1420 Maple Dr",
        client_id=agent_id,
        property_type="residential",
        address_line1=ADDRESS,
        city="Austin",
        state="TX",
        zip_code="78704",
        mls_id="ACT-2026-88421",
        beds=4,
        baths=3.5,
        sqft=2850,
        shoot_date=shoot_date,
        due_at=due_at,
        access_notes="Lockbox 4821 on side gate. Please remove shoes in primary suite.",
        notes=MARKER,
    )
    listings.update_listing(listing_id, status="delivered")
    db.run("UPDATE listings SET site_slug=? WHERE id=?", (SITE_SLUG, listing_id))
    microsites.update_site(
        listing_id,
        published=True,
        description="Sun-filled craftsman in Zilker — updated kitchen, primary down, walkable to Barton Springs.",
    )

    db.run("UPDATE listing_shots SET done=1 WHERE listing_id=?", (listing_id,))
    db.run("UPDATE listing_tasks SET done=1 WHERE listing_id=?", (listing_id,))

    appt_id = appointments.create_appointment(
        "1420 Maple Dr — shoot",
        kind="shoot",
        starts_at=starts_at,
        location=ADDRESS + ", Austin, TX 78704",
        listing_id=listing_id,
        client_id=agent_id,
    )
    appointments.update_appointment(appt_id, status="completed", ends_at=ends_at)

    gallery_id = galleries.create_gallery(
        "1420 Maple Dr",
        listing_id=listing_id,
        client_name="Sarah Chen",
    )
    galleries.get_gallery(gallery_id)
    galleries.update_gallery_settings(
        gallery_id,
        title="1420 Maple Dr",
        client_name="Sarah Chen",
        pin=GALLERY_PIN,
        expires_at=None,
        published=True,
        listing_id=listing_id,
    )
    _seed_photos(gallery_id)

    invoice_id = invoices.create_invoice(
        listing_id,
        title="Standard Listing — 1420 Maple Dr",
        amount_cents=17500,
        client_id=agent_id,
    )
    invoices.mark_sent(invoice_id)
    invoices.mark_paid(invoice_id)

    return _summary(listing_id, owner_email=email, owner_password=password)


def _summary(
    listing_id: int, *, owner_email: str | None = None, owner_password: str | None = None
) -> dict:
    listing = listings.get_listing(listing_id)
    gallery = db.one(
        "SELECT id, slug, pin FROM galleries WHERE listing_id=? ORDER BY id DESC LIMIT 1",
        (listing_id,),
    )
    agent = clients.get_client(listing["client_id"])
    broker = clients.get_client(agent["parent_id"]) if agent["parent_id"] else None
    appt = db.one(
        "SELECT id, starts_at FROM appointments WHERE listing_id=? ORDER BY id DESC LIMIT 1",
        (listing_id,),
    )
    base = config.BASE_URL.rstrip("/")
    out = {
        "listing_id": listing_id,
        "status": listing["status"],
        "admin_url": f"{base}/admin/listings/{listing_id}",
        "calendar_url": f"{base}/admin/calendar",
        "kanban_url": f"{base}/admin/kanban",
        "gallery_admin_url": f"{base}/admin/galleries/{gallery['id']}",
        "gallery_url": f"{base}/g/{gallery['slug']}",
        "gallery_pin": gallery["pin"],
        "site_url": f"{base}/l/{listing['site_slug']}",
        "agent_portal_url": f"{base}/portal/{agent['portal_token']}",
        "broker_portal_url": f"{base}/portal/brokerage/{broker['portal_token']}"
        if broker
        else None,
        "appointment_id": appt["id"] if appt else None,
        "appointment_at": appt["starts_at"] if appt else None,
        "agent_credit_cents": credits.balance(listing["client_id"]),
    }
    if owner_email:
        out["owner_email"] = owner_email
    if owner_password:
        out["owner_password"] = owner_password
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Seed Eos dogfood listing (1420 Maple Dr)")
    parser.add_argument("--force", action="store_true", help="Delete and re-seed dogfood data")
    parser.add_argument("--json", action="store_true", help="Print summary as JSON")
    args = parser.parse_args(argv)

    summary = seed(force=args.force)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("\nEos dogfood ready\n")
        for key, val in summary.items():
            if val is not None:
                print(f"  {key}: {val}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
