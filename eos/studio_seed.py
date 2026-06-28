"""Seed defaults for a new studio tenant."""

from . import db


def seed_studio(studio_id: str) -> None:
    db.run(
        "INSERT OR IGNORE INTO studio_profiles (studio_id) VALUES (?)",
        (studio_id,),
    )
    packages = [
        ("Standard Listing", "Up to 25 photos · 24hr turnaround", 17500, 5000, 24, 10),
        (
            "Premium Listing",
            "Up to 40 photos · twilight add-on · 24hr turnaround",
            27500,
            7500,
            24,
            20,
        ),
        (
            "Luxury Estate",
            "Full coverage · drone-ready naming · 12hr rush available",
            45000,
            10000,
            12,
            30,
        ),
    ]
    for name, desc, price, deposit, hours, pos in packages:
        db.run(
            """INSERT INTO service_packages
               (studio_id, name, description, price_cents, deposit_cents, turnaround_hours, position)
               VALUES (?,?,?,?,?,?,?)""",
            (studio_id, name, desc, price, deposit, hours, pos),
        )
    addons = [
        ("drone", "Aerial / drone photos", 10000, 10),
        ("twilight", "Twilight shoot", 15000, 20),
        ("matterport", "Matterport 3D tour", 12500, 30),
        ("rush", "Rush delivery (same day)", 7500, 40),
    ]
    for slug, name, price, pos in addons:
        db.run(
            """INSERT INTO service_addons (studio_id, slug, name, price_cents, position)
               VALUES (?,?,?,?,?)""",
            (studio_id, slug, name, price, pos),
        )
    presets = [
        ("mls-3x2", "MLS Standard (3:2)", "3:2", 2048, 1365, "mls", 10),
        ("zillow-16x9", "Zillow Hero (16:9)", "16:9", 1920, 1080, "zillow", 20),
        ("ig-4x5", "Instagram (4:5)", "4:5", 1080, 1350, "instagram", 30),
    ]
    for slug, name, ratio, w, h, channel, sort in presets:
        db.run(
            """INSERT INTO crop_presets
               (studio_id, slug, name, ratio_label, width, height, target_channel, sort)
               VALUES (?,?,?,?,?,?,?,?)""",
            (studio_id, slug, name, ratio, w, h, channel, sort),
        )
    sequences = [
        (
            "booking-confirm",
            "Booking confirmation",
            "listing.booked",
            0,
            "You're booked — {listing_title}",
            """Hi {client_first},

Your listing shoot is confirmed for {listing_title}.

Pre-shoot intake (please complete before we arrive):
{intake_link}

See you soon!
{site_name}""",
            10,
        ),
        (
            "delivery-followup",
            "Post-delivery follow-up",
            "listing.delivered",
            24,
            "Photos delivered — {listing_title}",
            """Hi {client_first},

Your gallery for {listing_title} is ready:
{gallery_link}
PIN: {gallery_pin}

Let me know if you need any MLS sizing tweaks.

Thank you!
{site_name}""",
            20,
        ),
        (
            "proposal-nudge",
            "Proposal reminder",
            "proposal.sent",
            48,
            "Following up — proposal for {listing_title}",
            """Hi {client_first},

Just checking in on the proposal for {listing_title}:

{proposal_link}

Happy to adjust the package if needed.

{site_name}""",
            30,
        ),
    ]
    for slug, name, trigger, delay, subject, body, pos in sequences:
        db.run(
            """INSERT INTO email_sequences
               (studio_id, slug, name, trigger_event, delay_hours, subject, body_template, position)
               VALUES (?,?,?,?,?,?,?,?)""",
            (studio_id, slug, name, trigger, delay, subject, body, pos),
        )
