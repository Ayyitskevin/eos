"""Owner reconciliation for ambiguous outbound delivery outcomes."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from eos import (
    db,
    delivery_notify,
    mailer,
    sequences,
    sms,
    tenant,
    users,
    webhooks,
)
from httpx import ASGITransport, AsyncClient

CHANNELS = ("gallery_email", "sequence_email", "sms", "webhook")

_UNKNOWN_ERRORS = {
    "gallery_email": "Delivery outcome unknown; verify the provider before retrying.",
    "sequence_email": "Email outcome unknown after SMTP dispatch; verify the provider.",
    "sms": "SMS outcome unknown after Twilio dispatch; verify Twilio before retrying.",
    "webhook": "Delivery outcome unknown; verify the endpoint before retrying.",
}
_TABLES = {
    "gallery_email": "delivery_notifications",
    "sequence_email": "email_sequence_runs",
    "sms": "sms_reminder_intents",
    "webhook": "webhook_deliveries",
}
_TERMINAL = {
    "gallery_email": "sent",
    "sequence_email": "sent",
    "sms": "sent",
    "webhook": "ok",
}
_RETRY_STATE = {
    "gallery_email": "pending",
    "sequence_email": "scheduled",
    "sms": "pending",
    "webhook": "pending",
}


def _ensure_studio(studio_id: str) -> None:
    if db.one("SELECT id FROM studio WHERE id=?", (studio_id,)):
        return
    db.run(
        "INSERT INTO studio (id, name, slug) VALUES (?,?,?)",
        (studio_id, f"{studio_id.title()} Photo", studio_id),
    )


def _client_listing(studio_id: str, suffix: str) -> tuple[int, int]:
    client_id = db.run(
        """INSERT INTO clients (studio_id, name, email, phone)
           VALUES (?,?,?,?)""",
        (
            studio_id,
            f"{studio_id.title()} Client",
            f"{suffix}@{studio_id}.test",
            "+15550001111",
        ),
    )
    listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status)
           VALUES (?,?,?,'booked')""",
        (studio_id, client_id, f"{studio_id.title()} Listing"),
    )
    return client_id, listing_id


def _seed(
    channel: str,
    *,
    studio_id: str = "default",
    state: str = "unknown",
) -> int:
    _ensure_studio(studio_id)
    error = _UNKNOWN_ERRORS[channel] if state in {"unknown", "active"} else "HTTP 503"
    claimed_at = "2026-07-26 12:00:00" if state == "active" else None
    attempt_token = "active-attempt" if state == "active" else None

    if channel == "gallery_email":
        _client_id, listing_id = _client_listing(studio_id, "gallery")
        gallery_id = db.run(
            """INSERT INTO galleries
               (studio_id, listing_id, slug, title, pin, delivery_token, published)
               VALUES (?,?,?,?,'1234',?,1)""",
            (
                studio_id,
                listing_id,
                f"gallery-{studio_id}",
                f"{studio_id.title()} Gallery",
                f"delivery-{studio_id}",
            ),
        )
        return db.run(
            """INSERT INTO delivery_notifications
               (studio_id, gallery_id, event_key, status, error, claimed_at, attempt_token)
               VALUES (?,?,?,?,?,?,?)""",
            (
                studio_id,
                gallery_id,
                f"listing:{listing_id}:delivered:r0",
                "pending" if state == "active" else "failed",
                error,
                claimed_at,
                attempt_token,
            ),
        )

    if channel == "sequence_email":
        client_id, listing_id = _client_listing(studio_id, "sequence")
        sequence_id = db.run(
            """INSERT INTO email_sequences
               (studio_id, slug, name, trigger_event, subject, body_template, channel)
               VALUES (?,?,?,'listing.booked','Booking confirmed','Hello','email')""",
            (
                studio_id,
                f"reconcile-{studio_id}",
                "Reconciliation sequence",
            ),
        )
        return db.run(
            """INSERT INTO email_sequence_runs
               (studio_id, sequence_id, listing_id, client_id, to_email, status,
                scheduled_at, event_key, error, claimed_at, attempt_token)
               VALUES (?,?,?,?,?,?,datetime('now','-1 minute'),?,?,?,?)""",
            (
                studio_id,
                sequence_id,
                listing_id,
                client_id,
                f"sequence@{studio_id}.test",
                "scheduled" if state == "active" else "failed",
                f"listing:{listing_id}:booked",
                error,
                claimed_at,
                attempt_token,
            ),
        )

    if channel == "sms":
        client_id, listing_id = _client_listing(studio_id, "sms")
        appointment_id = db.run(
            """INSERT INTO appointments
               (studio_id, listing_id, client_id, title, status, starts_at, token)
               VALUES (?,?,?,?,'confirmed','2026-08-01 10:00:00',?)""",
            (
                studio_id,
                listing_id,
                client_id,
                f"{studio_id.title()} Shoot",
                f"shoot-{studio_id}",
            ),
        )
        return db.run(
            """INSERT INTO sms_reminder_intents
               (studio_id, appointment_id, reminder_date, status, error,
                claimed_at, attempt_token)
               VALUES (?,?,'2026-08-01',?,?,?,?)""",
            (
                studio_id,
                appointment_id,
                "pending" if state == "active" else "failed",
                error,
                claimed_at,
                attempt_token,
            ),
        )

    subscription_id = db.run(
        """INSERT INTO webhook_subscriptions
           (studio_id, label, url, secret, events)
           VALUES (?,?,'https://hooks.example.test/eos','secret','["listing.delivered"]')""",
        (studio_id, f"{studio_id.title()} Hook"),
    )
    return db.run(
        """INSERT INTO webhook_deliveries
           (subscription_id, studio_id, event, status, error, event_key, payload,
            claimed_at, attempt_token, updated_at)
           VALUES (?,?,'listing.delivered',?,?,?,?,?,?,datetime('now'))""",
        (
            subscription_id,
            studio_id,
            "pending" if state == "active" else "failed",
            error,
            f"listing.delivered:{studio_id}",
            "{}",
            claimed_at,
            attempt_token,
        ),
    )


def _row(channel: str, row_id: int):
    return db.one(f"SELECT * FROM {_TABLES[channel]} WHERE id=?", (row_id,))


def _reconcile(channel: str, row_id: int, *, delivered: bool) -> None:
    if channel == "gallery_email":
        delivery_notify.reconcile_notification(row_id, delivered=delivered)
    elif channel == "sequence_email":
        sequences.reconcile_run(row_id, delivered=delivered)
    elif channel == "sms":
        sms.reconcile_reminder(row_id, delivered=delivered)
    else:
        webhooks.reconcile_delivery(row_id, delivered=delivered)


def _retry(channel: str, row_id: int) -> bool:
    if channel == "gallery_email":
        return delivery_notify.retry_notification(row_id)
    if channel == "sequence_email":
        return sequences.retry_run(row_id)
    if channel == "sms":
        return sms.retry_reminder(row_id)
    return webhooks.retry_delivery(row_id)


def _conflict(channel: str):
    return {
        "gallery_email": delivery_notify.DeliveryNotificationReconciliationConflict,
        "sequence_email": sequences.SequenceRunReconciliationConflict,
        "sms": sms.SmsReminderReconciliationConflict,
        "webhook": webhooks.WebhookDeliveryReconciliationConflict,
    }[channel]


def _not_found(channel: str):
    return {
        "gallery_email": delivery_notify.DeliveryNotificationNotFound,
        "sequence_email": sequences.SequenceRunNotFound,
        "sms": sms.SmsReminderNotFound,
        "webhook": webhooks.WebhookDeliveryNotFound,
    }[channel]


def _provider_guard(channel: str, monkeypatch) -> Mock:
    provider = Mock(side_effect=AssertionError("reconciliation contacted the provider"))
    if channel in {"gallery_email", "sequence_email"}:
        monkeypatch.setattr(mailer, "send_for_studio", provider)
    elif channel == "sms":
        monkeypatch.setattr(sms, "send", provider)
    else:
        monkeypatch.setattr(webhooks, "_post_pinned", provider)
        monkeypatch.setattr(webhooks, "_wake_delivery", Mock())
    return provider


def _evidence_count(channel: str, row_id: int) -> int:
    if channel == "gallery_email":
        return db.one(
            """SELECT COUNT(*) AS n FROM emails_log
               WHERE doc_kind='gallery_delivery' AND doc_id=?""",
            (row_id,),
        )["n"]
    if channel == "sequence_email":
        return db.one(
            "SELECT COUNT(*) AS n FROM emails_log WHERE doc_kind='sequence' AND doc_id=?",
            (row_id,),
        )["n"]
    if channel == "sms":
        return db.one("SELECT COUNT(*) AS n FROM sms_log WHERE status='sent'")["n"]
    return db.one(
        """SELECT COUNT(*) AS n FROM audit_log
           WHERE action='webhook.reconcile.delivered' AND detail=?""",
        (f"delivery={row_id}",),
    )["n"]


@pytest.mark.parametrize("channel", CHANNELS)
def test_provider_confirmed_delivered_is_terminal_and_records_evidence(
    app_env_http, monkeypatch, channel
):
    del app_env_http
    tenant.set_studio("default")
    row_id = _seed(channel)
    provider = _provider_guard(channel, monkeypatch)

    _reconcile(channel, row_id, delivered=True)

    row = _row(channel, row_id)
    assert row["status"] == _TERMINAL[channel]
    assert row["error"] is None
    assert row["claimed_at"] is None
    assert row["attempt_token"] is None
    assert _evidence_count(channel, row_id) == 1
    with pytest.raises(_conflict(channel)):
        _reconcile(channel, row_id, delivered=True)
    assert _evidence_count(channel, row_id) == 1
    provider.assert_not_called()


@pytest.mark.parametrize("channel", CHANNELS)
def test_provider_confirmed_not_delivered_becomes_explicitly_retryable(
    app_env_http, monkeypatch, channel
):
    del app_env_http
    tenant.set_studio("default")
    row_id = _seed(channel)
    provider = _provider_guard(channel, monkeypatch)

    assert _retry(channel, row_id) is False
    _reconcile(channel, row_id, delivered=False)
    failed = _row(channel, row_id)
    assert failed["status"] == "failed"
    assert "operator confirmed" in failed["error"].lower()
    assert "not delivered" in failed["error"].lower()
    provider.assert_not_called()

    assert _retry(channel, row_id) is True
    assert _row(channel, row_id)["status"] == _RETRY_STATE[channel]
    provider.assert_not_called()


@pytest.mark.parametrize("channel", CHANNELS)
def test_definite_failures_are_not_reconcilable(app_env_http, monkeypatch, channel):
    del app_env_http
    tenant.set_studio("default")
    row_id = _seed(channel, state="definite")
    provider = _provider_guard(channel, monkeypatch)

    with pytest.raises(_conflict(channel), match="only unknown"):
        _reconcile(channel, row_id, delivered=True)

    assert _row(channel, row_id)["status"] == "failed"
    assert _retry(channel, row_id) is True
    provider.assert_not_called()


@pytest.mark.parametrize("channel", CHANNELS)
def test_fresh_active_claims_are_not_reconcilable(app_env_http, monkeypatch, channel):
    del app_env_http
    tenant.set_studio("default")
    row_id = _seed(channel, state="active")
    provider = _provider_guard(channel, monkeypatch)

    with pytest.raises(_conflict(channel), match="active claim"):
        _reconcile(channel, row_id, delivered=True)

    assert _row(channel, row_id)["attempt_token"] == "active-attempt"
    assert _retry(channel, row_id) is False
    provider.assert_not_called()


@pytest.mark.parametrize("channel", CHANNELS)
def test_reconciliation_is_tenant_scoped(app_env_http, monkeypatch, channel):
    del app_env_http
    tenant.set_studio("beta")
    row_id = _seed(channel, studio_id="beta")
    tenant.set_studio("default")
    provider = _provider_guard(channel, monkeypatch)

    with pytest.raises(_not_found(channel)):
        _reconcile(channel, row_id, delivered=True)

    assert _row(channel, row_id)["status"] == "failed"
    provider.assert_not_called()


def _route(channel: str, row_id: int) -> str:
    return {
        "gallery_email": f"/admin/studio/delivery-notifications/{row_id}/reconcile",
        "sequence_email": f"/admin/sequences/runs/{row_id}/reconcile",
        "sms": f"/admin/studio/sms-reminders/{row_id}/reconcile",
        "webhook": f"/admin/studio/webhook-deliveries/{row_id}/reconcile",
    }[channel]


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", CHANNELS)
async def test_reconciliation_http_control_is_owner_only_and_csrf_protected(
    app_env_http, monkeypatch, channel
):
    tenant.set_studio("default")
    row_id = _seed(channel)
    provider = _provider_guard(channel, monkeypatch)
    users.create_user(
        "operator@default.test",
        "operator-pass-1",
        role="operator",
        studio_id="default",
    )
    users.create_user(
        "owner@default.test",
        "owner-pass-1",
        role="owner",
        studio_id="default",
    )
    route = _route(channel, row_id)
    transport = ASGITransport(app=app_env_http)

    async with AsyncClient(transport=transport, base_url="http://testserver") as operator:
        login = await operator.post(
            "/admin/login",
            data={"email": "operator@default.test", "password": "operator-pass-1"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        blocked = await operator.post(
            route,
            data={"outcome": "delivered", "_csrf": operator.cookies.get("eos_csrf")},
            headers={"sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert blocked.status_code == 403

    async with AsyncClient(transport=transport, base_url="http://testserver") as owner:
        login = await owner.post(
            "/admin/login",
            data={"email": "owner@default.test", "password": "owner-pass-1"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        page_path = "/admin/sequences" if channel == "sequence_email" else "/admin/studio"
        page = await owner.get(page_path)
        assert page.status_code == 200
        assert "confirms delivered" in page.text
        assert "confirms not delivered" in page.text

        missing_csrf = await owner.post(
            route,
            data={"outcome": "delivered"},
            headers={"sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert missing_csrf.status_code == 403
        allowed = await owner.post(
            route,
            data={"outcome": "delivered", "_csrf": owner.cookies.get("eos_csrf")},
            headers={"sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert allowed.status_code == 303

    assert _row(channel, row_id)["status"] == _TERMINAL[channel]
    provider.assert_not_called()
