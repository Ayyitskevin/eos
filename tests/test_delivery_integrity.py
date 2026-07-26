"""Acceptance tests for the central gallery delivery transaction."""

from __future__ import annotations

import importlib
import json
from typing import Any
from unittest.mock import Mock

import eos.automations as automations
import eos.config as config
import eos.db as db
import eos.delivery_notify as delivery_notify
import eos.emails as emails
import eos.galleries as galleries
import eos.jobs as jobs
import eos.listings as listings
import eos.main as main
import eos.media_paths as media_paths
import eos.portal as portal
import eos.referrals as referrals
import eos.routes.portal as portal_routes
import eos.security as security
import eos.sequences as sequences
import eos.studio_seed as studio_seed
import eos.tenant as tenant
import eos.webhooks as webhooks
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

STUDIO_ID = "alpha"
TENANT_HOST = "alpha.eos.test"
TENANT_ORIGIN = f"http://{TENANT_HOST}"


@pytest.fixture()
def delivery_env(tmp_path, monkeypatch):
    """Fresh tenant with external delivery providers disabled."""
    values = {
        "EOS_DATA_DIR": str(tmp_path / "data"),
        "EOS_SECRET_KEY": "test-secret-key-32chars-minimum!!",
        "EOS_ADMIN_PASSWORD": "test-admin-pass",
        "EOS_BASE_URL": "http://eos.test",
        "EOS_BASE_DOMAIN": "eos.test",
        "EOS_SAAS_MODE": "false",
        "EOS_SIGNUP_ENABLED": "false",
        "EOS_BILLING_ENFORCE": "false",
        "EOS_DEMO_ENABLED": "false",
        "EOS_GMAIL_USER": "",
        "EOS_GMAIL_APP_PASSWORD": "",
        "EOS_POSTMARK_API_KEY": "",
        "EOS_POSTMARK_FROM_EMAIL": "",
        "EOS_STRIPE_SECRET_KEY": "",
        "EOS_STRIPE_WEBHOOK_SECRET": "",
        "EOS_STRIPE_PLATFORM_SECRET_KEY": "",
        "EOS_STRIPE_PLATFORM_WEBHOOK_SECRET": "",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    for module in (
        config,
        db,
        security,
        tenant,
        emails,
        referrals,
        portal,
        sequences,
        jobs,
        webhooks,
        delivery_notify,
        automations,
        listings,
        galleries,
        portal_routes,
        main,
    ):
        importlib.reload(module)

    config.ensure_dirs()
    db.migrate()
    db.run(
        """INSERT INTO studio (id, name, slug, contact_email)
           VALUES (?, 'Alpha Photo', ?, 'owner@alpha.test')""",
        (STUDIO_ID, STUDIO_ID),
    )
    studio_seed.seed_studio(STUDIO_ID)
    tenant.set_studio(STUDIO_ID)
    db.run(
        """UPDATE studio_profiles
           SET auto_deliver_email=1, auto_publish_site=1
           WHERE studio_id=?""",
        (STUDIO_ID,),
    )

    def reject_network(*_args, **_kwargs):
        raise AssertionError("external provider access is forbidden in delivery tests")

    monkeypatch.setattr(webhooks, "_wake_delivery", lambda _delivery_id: None)
    monkeypatch.setattr(webhooks.httpx, "post", reject_network)
    monkeypatch.setattr(delivery_notify.mailer, "send_for_studio", reject_network)
    yield main.app
    tenant.set_studio("default")


def _seed_delivery(
    *,
    listing_status: str = "editing",
    asset_statuses: tuple[str, ...] = ("ready",),
    shoot_status: str | None = "completed",
    subscriptions: int = 2,
) -> dict[str, Any]:
    client_id = db.run(
        """INSERT INTO clients (studio_id, name, email)
           VALUES (?, 'Agent Ada', 'ada@alpha.test')""",
        (STUDIO_ID,),
    )
    listing_id = db.run(
        """INSERT INTO listings
           (studio_id, client_id, title, status, address_line1, city, state)
           VALUES (?, ?, '17 Integrity Lane', ?, '17 Integrity Lane', 'Albany', 'NY')""",
        (STUDIO_ID, client_id, listing_status),
    )
    gallery_id = db.run(
        """INSERT INTO galleries
           (studio_id, listing_id, slug, title, client_name, pin, delivery_token)
           VALUES (?, ?, 'integrity-gallery', 'Integrity Gallery',
                   'Agent Ada', '1234', 'delivery-integrity-token')""",
        (STUDIO_ID, listing_id),
    )
    for position, status in enumerate(asset_statuses):
        db.run(
            """INSERT INTO assets
               (gallery_id, kind, filename, stored, status, position)
               VALUES (?, 'photo', ?, ?, ?, ?)""",
            (
                gallery_id,
                f"photo-{position}.jpg",
                f"photo-{position}.jpg",
                status,
                position,
            ),
        )
    if shoot_status is not None:
        db.run(
            """INSERT INTO appointments
               (studio_id, listing_id, client_id, title, kind, status, starts_at, token)
               VALUES (?, ?, ?, 'Listing shoot', 'shoot', ?, '2026-08-01 10:00',
                       'integrity-shoot-token')""",
            (STUDIO_ID, listing_id, client_id, shoot_status),
        )

    subscription_ids = []
    for number in range(subscriptions):
        subscription_ids.append(
            db.run(
                """INSERT INTO webhook_subscriptions
                   (studio_id, label, url, secret, events)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    STUDIO_ID,
                    f"Delivery hook {number}",
                    f"https://hooks.invalid/{number}",
                    f"secret-{number}",
                    json.dumps(["listing.delivered"]),
                ),
            )
        )
    return {
        "client_id": client_id,
        "listing_id": listing_id,
        "gallery_id": gallery_id,
        "subscription_ids": subscription_ids,
    }


def _publish(seed: dict[str, Any], *, published: bool = True) -> bool:
    return galleries.update_gallery_settings(
        seed["gallery_id"],
        title="Integrity Gallery",
        client_name="Agent Ada",
        pin="1234",
        expires_at="2026-12-31",
        published=published,
        listing_id=seed["listing_id"],
    )


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in db.all_(sql, params)]


def _delivery_state(seed: dict[str, Any]) -> dict[str, Any]:
    gallery_id = seed["gallery_id"]
    listing_id = seed["listing_id"]
    client_id = seed["client_id"]
    return {
        "gallery": dict(db.one("SELECT * FROM galleries WHERE id=?", (gallery_id,))),
        "listing": dict(db.one("SELECT * FROM listings WHERE id=?", (listing_id,))),
        "client": dict(db.one("SELECT * FROM clients WHERE id=?", (client_id,))),
        "referrals": _rows(
            """SELECT * FROM referral_codes
               WHERE studio_id=? AND referrer_client_id=? ORDER BY id""",
            (STUDIO_ID, client_id),
        ),
        "sequences": _rows(
            """SELECT * FROM email_sequence_runs
               WHERE studio_id=? AND listing_id=? ORDER BY id""",
            (STUDIO_ID, listing_id),
        ),
        "notifications": _rows(
            """SELECT * FROM delivery_notifications
               WHERE studio_id=? AND gallery_id=? ORDER BY id""",
            (STUDIO_ID, gallery_id),
        ),
        "webhooks": _rows(
            """SELECT * FROM webhook_deliveries
               WHERE studio_id=? ORDER BY subscription_id, id""",
            (STUDIO_ID,),
        ),
        "jobs": _rows(
            """SELECT * FROM jobs
               WHERE kind='gallery_exports' ORDER BY id""",
        ),
    }


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("no_assets", "assets_required"),
        ("pending_asset", "assets_not_ready"),
        ("failed_asset", "assets_not_ready"),
        ("lead_listing", "listing_not_ready"),
        ("archived_listing", "listing_not_ready"),
        ("incomplete_shoot", "shoot_not_complete"),
    ),
)
def test_publication_rejects_incomplete_delivery_without_mutation(delivery_env, case, reason):
    del delivery_env
    kwargs: dict[str, Any] = {}
    if case == "no_assets":
        kwargs.update(asset_statuses=(), shoot_status=None)
    elif case == "pending_asset":
        kwargs.update(asset_statuses=("pending",), shoot_status=None)
    elif case == "failed_asset":
        kwargs.update(asset_statuses=("failed",), shoot_status=None)
    elif case == "lead_listing":
        kwargs.update(listing_status="lead", shoot_status=None)
    elif case == "archived_listing":
        kwargs.update(listing_status="archived", shoot_status=None)
    else:
        kwargs.update(shoot_status="confirmed")

    seed = _seed_delivery(**kwargs)
    before = _delivery_state(seed)

    with pytest.raises(HTTPException) as exc:
        galleries.update_gallery_settings(
            seed["gallery_id"],
            title="Should Not Publish",
            client_name="Changed Client",
            pin="9876",
            expires_at="2027-01-01",
            published=True,
            listing_id=seed["listing_id"],
        )

    assert exc.value.status_code == 409
    assert reason in str(exc.value.detail)
    assert _delivery_state(seed) == before


def test_late_delivery_effect_failure_rolls_back_everything(delivery_env, monkeypatch):
    del delivery_env
    seed = _seed_delivery(asset_statuses=("ready", "ready"))
    before = _delivery_state(seed)

    def fail_exports(*_args, **_kwargs):
        raise RuntimeError("export enqueue failed")

    monkeypatch.setattr(jobs, "enqueue", fail_exports)
    with pytest.raises(RuntimeError, match="export enqueue failed"):
        _publish(seed)

    assert _delivery_state(seed) == before


def test_publish_replay_and_republish_create_one_delivery_effect_set(delivery_env):
    del delivery_env
    seed = _seed_delivery(asset_statuses=("ready", "ready"))

    assert _publish(seed) is True
    first = _delivery_state(seed)
    gallery = first["gallery"]
    listing = first["listing"]

    assert gallery["published"] == 1
    assert listing["status"] == "delivered"
    assert listing["delivered_at"]
    assert listing["site_slug"]
    assert listing["site_published"] == 1
    assert first["client"]["portal_token"]

    assert len(first["referrals"]) == 1
    assert first["referrals"][0]["referrer_client_id"] == seed["client_id"]

    assert len(first["sequences"]) == 1
    assert first["sequences"][0]["event_key"] == (f"listing:{seed['listing_id']}:delivered:r0")

    assert len(first["notifications"]) == 1
    assert first["notifications"][0]["status"] == "pending"

    assert len(first["webhooks"]) == len(seed["subscription_ids"])
    assert {row["subscription_id"] for row in first["webhooks"]} == set(seed["subscription_ids"])
    assert all(row["event"] == "listing.delivered" for row in first["webhooks"])
    assert all(
        row["event_key"] == f"listing:{seed['listing_id']}:delivered:r0"
        for row in first["webhooks"]
    )

    assert len(first["jobs"]) == 1
    assert first["jobs"][0]["idempotency_key"] == (
        f"gallery:{seed['gallery_id']}:delivery-exports:r0"
    )

    first_intents = {
        key: first[key] for key in ("referrals", "sequences", "notifications", "webhooks", "jobs")
    }
    delivered_at = listing["delivered_at"]

    assert _publish(seed) is False
    assert _publish(seed, published=False) is False
    unpublished = _delivery_state(seed)
    assert unpublished["gallery"]["published"] == 0
    assert unpublished["listing"]["status"] == "delivered"
    assert unpublished["listing"]["delivered_at"] == delivered_at

    assert _publish(seed) is True
    replayed = _delivery_state(seed)
    assert replayed["gallery"]["published"] == 1
    assert replayed["listing"]["status"] == "delivered"
    assert replayed["listing"]["delivered_at"] == delivered_at
    assert {
        key: replayed[key]
        for key in ("referrals", "sequences", "notifications", "webhooks", "jobs")
    } == first_intents


def test_revision_redelivery_creates_one_effect_set_per_round(delivery_env):
    del delivery_env
    seed = _seed_delivery(asset_statuses=("ready", "ready"))
    _publish(seed)

    listings.request_revision(seed["listing_id"], notes="Replace the twilight frame")
    revised = db.one(
        "SELECT status, revision_round FROM listings WHERE id=?",
        (seed["listing_id"],),
    )
    assert dict(revised) == {"status": "editing", "revision_round": 1}

    assert galleries.redeliver_listing(seed["listing_id"]) == seed["gallery_id"]
    second = _delivery_state(seed)
    expected = {
        f"listing:{seed['listing_id']}:delivered:r0",
        f"listing:{seed['listing_id']}:delivered:r1",
    }
    assert {row["event_key"] for row in second["sequences"]} == expected
    assert {row["event_key"] for row in second["notifications"]} == expected
    assert {row["event_key"] for row in second["webhooks"]} == expected
    assert {row["idempotency_key"] for row in second["jobs"]} == {
        f"gallery:{seed['gallery_id']}:delivery-exports:r0",
        f"gallery:{seed['gallery_id']}:delivery-exports:r1",
    }

    effect_rows = {key: second[key] for key in ("sequences", "notifications", "webhooks", "jobs")}
    assert galleries.redeliver_listing(seed["listing_id"]) == seed["gallery_id"]
    replayed = _delivery_state(seed)
    assert {
        key: replayed[key] for key in ("sequences", "notifications", "webhooks", "jobs")
    } == effect_rows


def test_published_or_delivered_gallery_cannot_be_relinked(delivery_env):
    del delivery_env
    seed = _seed_delivery()
    _publish(seed)
    _publish(seed, published=False)
    second_listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, address_line1)
           VALUES (?, ?, 'Second listing', 'editing', '2 Integrity Lane')""",
        (STUDIO_ID, seed["client_id"]),
    )

    with pytest.raises(HTTPException, match="cannot be linked") as exc_info:
        galleries.update_gallery_settings(
            seed["gallery_id"],
            title="Integrity Gallery",
            client_name="Agent Ada",
            pin="1234",
            expires_at="2026-12-31",
            published=False,
            listing_id=second_listing_id,
        )

    assert exc_info.value.status_code == 409
    row = db.one(
        "SELECT listing_id, delivered_at FROM galleries WHERE id=?",
        (seed["gallery_id"],),
    )
    assert row["listing_id"] == seed["listing_id"]
    assert row["delivered_at"]


def _write_original(seed: dict[str, Any]) -> int:
    asset = db.one(
        "SELECT id, stored FROM assets WHERE gallery_id=? ORDER BY id LIMIT 1",
        (seed["gallery_id"],),
    )
    original = media_paths.gallery_dir(seed["gallery_id"]) / "original" / asset["stored"]
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"original-photo")
    return int(asset["id"])


@pytest.mark.asyncio
async def test_pin_rotation_invalidates_existing_gallery_capability(delivery_env):
    seed = _seed_delivery(subscriptions=0)
    _publish(seed)
    asset_id = _write_original(seed)
    transport = ASGITransport(app=delivery_env)

    async with AsyncClient(transport=transport, base_url=TENANT_ORIGIN) as client:
        unlocked = await client.post(
            "/g/integrity-gallery/pin",
            data={"pin": "1234"},
            follow_redirects=False,
        )
        assert unlocked.status_code == 303
        before = await client.get(f"/media/integrity-gallery/original/{asset_id}")
        assert before.status_code == 200

        galleries.update_gallery_settings(
            seed["gallery_id"],
            title="Integrity Gallery",
            client_name="Agent Ada",
            pin="5678",
            expires_at="2026-12-31",
            published=True,
            listing_id=seed["listing_id"],
        )
        after = await client.get(f"/media/integrity-gallery/original/{asset_id}")

    assert after.status_code == 403


@pytest.mark.asyncio
async def test_expiry_and_payment_gate_protect_direct_original_routes(delivery_env):
    seed = _seed_delivery(subscriptions=0)
    _publish(seed)
    asset_id = _write_original(seed)
    transport = ASGITransport(app=delivery_env)

    async with AsyncClient(transport=transport, base_url=TENANT_ORIGIN) as client:
        assert (
            await client.post(
                "/g/integrity-gallery/pin",
                data={"pin": "1234"},
                follow_redirects=False,
            )
        ).status_code == 303
        invoice_id = db.run(
            """INSERT INTO invoices
               (studio_id, listing_id, client_id, slug, title, amount_cents, status, line_items)
               VALUES (?, ?, ?, 'integrity-unpaid', 'Balance', 25000, 'sent', '[]')""",
            (STUDIO_ID, seed["listing_id"], seed["client_id"]),
        )
        locked = await client.get(f"/media/integrity-gallery/original/{asset_id}")
        assert locked.status_code == 402

        db.run("UPDATE invoices SET status='paid' WHERE id=?", (invoice_id,))
        assert (
            await client.get(f"/media/integrity-gallery/original/{asset_id}")
        ).status_code == 200

        db.run(
            "UPDATE galleries SET expires_at='2020-01-01' WHERE id=?",
            (seed["gallery_id"],),
        )
        expired_media = await client.get(f"/media/integrity-gallery/original/{asset_id}")
        expired_zip = await client.get("/g/integrity-gallery/download/zip")

    assert expired_media.status_code == 410
    assert expired_zip.status_code == 410


def test_delivered_listing_cannot_regress(delivery_env):
    del delivery_env
    seed = _seed_delivery()
    _publish(seed)
    delivered = dict(db.one("SELECT * FROM listings WHERE id=?", (seed["listing_id"],)))

    for status in ("lead", "booked", "shooting", "editing"):
        with pytest.raises(HTTPException) as exc:
            listings.update_listing(seed["listing_id"], status=status)
        assert exc.value.status_code == 409
        current = dict(db.one("SELECT * FROM listings WHERE id=?", (seed["listing_id"],)))
        assert current == delivered


@pytest.mark.asyncio
async def test_portal_and_delivery_email_use_tenant_repeat_links(delivery_env, monkeypatch):
    seed = _seed_delivery(subscriptions=0)
    _publish(seed)
    client = db.one("SELECT * FROM clients WHERE id=?", (seed["client_id"],))
    referral = db.one(
        """SELECT * FROM referral_codes
           WHERE studio_id=? AND referrer_client_id=?""",
        (STUDIO_ID, seed["client_id"]),
    )
    expected_rebook = f"{TENANT_ORIGIN}/book?returning={client['portal_token']}"
    expected_referral = f"{TENANT_ORIGIN}/r/{referral['code']}"
    delivery_sequence = db.one(
        """SELECT s.body_template FROM email_sequence_runs r
           JOIN email_sequences s ON s.id=r.sequence_id AND s.studio_id=r.studio_id
           WHERE r.studio_id=? AND r.listing_id=? AND s.trigger_event='listing.delivered'""",
        (STUDIO_ID, seed["listing_id"]),
    )
    sequence_body = sequences.render_template(
        delivery_sequence["body_template"],
        sequences.build_context(seed["listing_id"]),
    )
    assert expected_rebook in sequence_body
    assert expected_referral in sequence_body

    notification = db.one(
        """SELECT * FROM delivery_notifications
           WHERE studio_id=? AND gallery_id=?""",
        (STUDIO_ID, seed["gallery_id"]),
    )
    send = Mock()
    monkeypatch.setattr(delivery_notify.mailer, "configured", lambda: True)
    monkeypatch.setattr(delivery_notify.mailer, "send_for_studio", send)
    assert delivery_notify.process_notification(notification["id"]) is True
    send.assert_called_once()
    email_body = send.call_args.args[2]
    assert expected_rebook in email_body
    assert expected_referral in email_body

    transport = ASGITransport(app=delivery_env)
    async with AsyncClient(
        transport=transport,
        base_url=TENANT_ORIGIN,
    ) as client_http:
        response = await client_http.get(f"/portal/{client['portal_token']}")

    assert response.status_code == 200
    assert expected_rebook in response.text
    assert expected_referral in response.text


def test_direct_listing_delivery_cannot_bypass_gallery_readiness(delivery_env):
    del delivery_env
    seed = _seed_delivery(asset_statuses=("ready",), shoot_status="completed")
    before = _delivery_state(seed)

    with pytest.raises(HTTPException) as exc:
        listings.update_listing(seed["listing_id"], status="delivered")

    assert exc.value.status_code == 409
    assert "Publish a ready gallery" in str(exc.value.detail)
    assert _delivery_state(seed) == before
