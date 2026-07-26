"""Recovery tests for durable outbound email and SMS state machines."""

from __future__ import annotations

import datetime as dt
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import eos.config as config
import eos.db as db
import eos.delivery_notify as delivery_notify
import eos.mailer as mailer
import eos.sequences as sequences
import eos.sms as sms
import eos.tenant as tenant
import pytest


@pytest.fixture()
def outbound_env(tmp_path, monkeypatch):
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
        "EOS_TWILIO_ACCOUNT_SID": "",
        "EOS_TWILIO_AUTH_TOKEN": "",
        "EOS_TWILIO_FROM_NUMBER": "",
        "EOS_TIMEZONE": "America/New_York",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    for module in (config, db, tenant, mailer, delivery_notify, sequences, sms):
        importlib.reload(module)
    config.ensure_dirs()
    db.migrate()
    tenant.set_studio("default")
    yield
    tenant.set_studio("default")


def _add_studio(studio_id: str, name: str) -> None:
    if studio_id == "default":
        db.run("UPDATE studio SET name=? WHERE id='default'", (name,))
        return
    db.run(
        "INSERT INTO studio (id, name, slug) VALUES (?,?,?)",
        (studio_id, name, studio_id),
    )


def _add_client(studio_id: str, *, email: str, phone: str = "") -> int:
    return db.run(
        """INSERT INTO clients (studio_id, name, email, phone)
           VALUES (?, ?, ?, ?)""",
        (studio_id, f"{studio_id.title()} Agent", email, phone),
    )


def _add_listing(studio_id: str, client_id: int) -> int:
    return db.run(
        """INSERT INTO listings (studio_id, client_id, title, status)
           VALUES (?, ?, ?, 'booked')""",
        (studio_id, client_id, f"{studio_id.title()} Listing"),
    )


def _add_delivery_notification(studio_id: str = "default") -> int:
    client_id = _add_client(studio_id, email=f"agent@{studio_id}.test")
    listing_id = _add_listing(studio_id, client_id)
    gallery_id = db.run(
        """INSERT INTO galleries
           (studio_id, listing_id, slug, title, pin, delivery_token, published)
           VALUES (?, ?, ?, ?, '1234', ?, 1)""",
        (
            studio_id,
            listing_id,
            f"{studio_id}-gallery",
            f"{studio_id.title()} Gallery",
            f"{studio_id}-delivery-token",
        ),
    )
    return db.run(
        """INSERT INTO delivery_notifications
           (studio_id, gallery_id, event_key, status)
           VALUES (?, ?, ?, 'pending')""",
        (studio_id, gallery_id, f"listing:{listing_id}:delivered:r0"),
    )


def _add_sequence_run(studio_id: str = "default") -> int:
    client_id = _add_client(studio_id, email=f"sequence@{studio_id}.test")
    listing_id = _add_listing(studio_id, client_id)
    sequence_id = db.run(
        """INSERT INTO email_sequences
           (studio_id, slug, name, trigger_event, subject, body_template)
           VALUES (?, ?, 'Recovery sequence', 'listing.booked',
                   'Booked {listing_title}', 'Hi {client_first}')""",
        (studio_id, f"{studio_id}-recovery"),
    )
    return db.run(
        """INSERT INTO email_sequence_runs
           (studio_id, sequence_id, listing_id, client_id, to_email,
            status, scheduled_at, event_key)
           VALUES (?, ?, ?, ?, ?, 'scheduled', datetime('now', '-1 minute'), ?)""",
        (
            studio_id,
            sequence_id,
            listing_id,
            client_id,
            f"sequence@{studio_id}.test",
            f"listing:{listing_id}:booked",
        ),
    )


def _add_appointment(studio_id: str, reminder_date: str, *, phone: str) -> int:
    client_id = _add_client(
        studio_id,
        email=f"reminder@{studio_id}.test",
        phone=phone,
    )
    return db.run(
        """INSERT INTO appointments
           (studio_id, client_id, title, kind, status, starts_at, token)
           VALUES (?, ?, ?, 'shoot', 'confirmed', ?, ?)""",
        (
            studio_id,
            client_id,
            f"{studio_id.title()} Shoot",
            f"{reminder_date} 10:00:00",
            f"{studio_id}-{reminder_date}-shoot",
        ),
    )


def _row(table: str, row_id: int):
    return db.one(f"SELECT * FROM {table} WHERE id=?", (row_id,))


def test_stale_claims_fail_closed_and_are_not_automatically_retried(outbound_env, monkeypatch):
    del outbound_env
    reminder_date = "2026-08-02"
    notification_id = _add_delivery_notification()
    run_id = _add_sequence_run()
    appointment_id = _add_appointment("default", reminder_date, phone="+15550001000")
    intent_id = db.run(
        """INSERT INTO sms_reminder_intents
           (studio_id, appointment_id, reminder_date, reminder_kind, attempts, claimed_at)
           VALUES ('default', ?, ?, 'shoot_day', 1, '2000-01-01 00:00:00')""",
        (appointment_id, reminder_date),
    )
    db.run(
        """UPDATE delivery_notifications
           SET attempts=1, claimed_at='2000-01-01 00:00:00' WHERE id=?""",
        (notification_id,),
    )
    db.run(
        """UPDATE email_sequence_runs
           SET attempts=1, claimed_at='2000-01-01 00:00:00' WHERE id=?""",
        (run_id,),
    )

    email_send = Mock()
    sms_send = Mock(return_value=True)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send_for_studio", email_send)
    monkeypatch.setattr(sms, "configured", lambda: True)
    monkeypatch.setattr(sms, "_local_date", lambda: reminder_date)
    monkeypatch.setattr(sms, "send", sms_send)

    assert delivery_notify.process_pending() == 0
    assert sequences.process_due() == 0
    assert sms.shoot_day_reminders() == 0

    for table, row_id in (
        ("delivery_notifications", notification_id),
        ("email_sequence_runs", run_id),
        ("sms_reminder_intents", intent_id),
    ):
        row = _row(table, row_id)
        assert row["status"] == "failed"
        assert row["claimed_at"] is None
        assert "unknown" in row["error"].lower()
        assert "verify" in row["error"].lower()
        assert row["attempts"] == 1

    assert delivery_notify.process_pending() == 0
    assert sequences.process_due() == 0
    assert sms.shoot_day_reminders() == 0
    email_send.assert_not_called()
    sms_send.assert_not_called()


def test_delivery_provider_unknown_requires_operator_review(outbound_env, monkeypatch):
    del outbound_env
    notification_id = _add_delivery_notification()
    send = Mock(
        side_effect=mailer.DeliveryOutcomeUnknown(
            "Email outcome unknown after provider dispatch; verify provider."
        )
    )
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send_for_studio", send)

    assert delivery_notify.process_notification(notification_id) is False
    row = _row("delivery_notifications", notification_id)
    assert row["status"] == "failed"
    assert row["claimed_at"] is None
    assert row["attempts"] == 1
    assert "unknown" in row["error"].lower()
    assert "verify" in row["error"].lower()

    assert delivery_notify.process_pending() == 0
    send.assert_called_once()


def test_delivery_provider_success_then_local_failure_stays_closed(outbound_env, monkeypatch):
    del outbound_env
    notification_id = _add_delivery_notification()
    db.run(
        """CREATE TRIGGER fail_gallery_delivery_log
           BEFORE INSERT ON emails_log
           WHEN NEW.doc_kind='gallery_delivery'
           BEGIN
             SELECT RAISE(ABORT, 'simulated delivery persistence failure');
           END"""
    )
    send = Mock()
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send_for_studio", send)

    assert delivery_notify.process_notification(notification_id) is False
    row = _row("delivery_notifications", notification_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert "unknown" in row["error"].lower()
    assert "simulated delivery persistence failure" in row["error"]
    assert (
        db.one("SELECT COUNT(*) AS n FROM emails_log WHERE doc_kind='gallery_delivery'")["n"] == 0
    )

    assert delivery_notify.process_pending() == 0
    send.assert_called_once()


def test_sequence_provider_success_then_local_failure_stays_closed(outbound_env, monkeypatch):
    del outbound_env
    run_id = _add_sequence_run()
    db.run(
        """CREATE TRIGGER fail_sequence_log
           BEFORE INSERT ON emails_log
           WHEN NEW.doc_kind='sequence'
           BEGIN
             SELECT RAISE(ABORT, 'simulated sequence persistence failure');
           END"""
    )
    send = Mock()
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send_for_studio", send)

    assert sequences.process_due() == 0
    row = _row("email_sequence_runs", run_id)
    assert row["status"] == "failed"
    assert row["claimed_at"] is None
    assert row["attempts"] == 1
    assert "unknown" in row["error"].lower()
    assert "simulated sequence persistence failure" in row["error"]
    assert db.one("SELECT COUNT(*) AS n FROM emails_log WHERE doc_kind='sequence'")["n"] == 0

    assert sequences.process_due() == 0
    send.assert_called_once()


def test_repeated_sms_tick_sends_once_and_binds_each_tenant(outbound_env, monkeypatch):
    del outbound_env
    reminder_date = "2026-08-03"
    _add_studio("alpha", "Alpha Photo")
    _add_studio("beta", "Beta Photo")
    _add_appointment("alpha", reminder_date, phone="+15550001111")
    _add_appointment("beta", reminder_date, phone="+15550002222")
    tenant.set_studio("default")

    sends: list[tuple[str, str, str]] = []

    def record_send(*, to_phone: str, body: str) -> bool:
        sends.append((tenant.get_studio_id(), to_phone, body))
        return True

    monkeypatch.setattr(sms, "configured", lambda: True)
    monkeypatch.setattr(sms, "_local_date", lambda: reminder_date)
    monkeypatch.setattr(sms, "send", record_send)

    assert sms.shoot_day_reminders() == 2
    assert sms.shoot_day_reminders() == 0
    assert tenant.get_studio_id() == "default"
    assert {(studio_id, phone) for studio_id, phone, _body in sends} == {
        ("alpha", "+15550001111"),
        ("beta", "+15550002222"),
    }
    assert any("Alpha Photo" in body for studio_id, _phone, body in sends if studio_id == "alpha")
    assert any("Beta Photo" in body for studio_id, _phone, body in sends if studio_id == "beta")

    intents = db.all_(
        """SELECT studio_id, appointment_id, reminder_date, reminder_kind, status, attempts
           FROM sms_reminder_intents ORDER BY studio_id"""
    )
    assert [row["studio_id"] for row in intents] == ["alpha", "beta"]
    assert all(row["reminder_date"] == reminder_date for row in intents)
    assert all(row["reminder_kind"] == "shoot_day" for row in intents)
    assert all(row["status"] == "sent" and row["attempts"] == 1 for row in intents)


def test_sms_provider_success_then_local_failure_stays_closed(outbound_env, monkeypatch):
    del outbound_env
    reminder_date = "2026-08-04"
    appointment_id = _add_appointment("default", reminder_date, phone="+15550003333")
    db.run(
        """CREATE TRIGGER fail_sms_sent_transition
           BEFORE UPDATE OF status ON sms_reminder_intents
           WHEN NEW.status='sent'
           BEGIN
             SELECT RAISE(ABORT, 'simulated SMS persistence failure');
           END"""
    )
    send = Mock(return_value=True)
    monkeypatch.setattr(sms, "configured", lambda: True)
    monkeypatch.setattr(sms, "_local_date", lambda: reminder_date)
    monkeypatch.setattr(sms, "send", send)

    assert sms.shoot_day_reminders() == 0
    row = db.one(
        """SELECT * FROM sms_reminder_intents
           WHERE appointment_id=? AND reminder_date=?""",
        (appointment_id, reminder_date),
    )
    assert row["status"] == "failed"
    assert row["claimed_at"] is None
    assert row["attempts"] == 1
    assert "unknown" in row["error"].lower()
    assert "simulated SMS persistence failure" in row["error"]

    assert sms.shoot_day_reminders() == 0
    send.assert_called_once()


def test_sms_provider_unknown_requires_operator_review(outbound_env, monkeypatch):
    del outbound_env
    reminder_date = "2026-08-05"
    appointment_id = _add_appointment("default", reminder_date, phone="+15550004444")
    send = Mock(
        side_effect=sms.SmsOutcomeUnknown(
            "SMS outcome unknown after Twilio acceptance; verify Twilio."
        )
    )
    monkeypatch.setattr(sms, "configured", lambda: True)
    monkeypatch.setattr(sms, "_local_date", lambda: reminder_date)
    monkeypatch.setattr(sms, "send", send)

    assert sms.shoot_day_reminders() == 0
    row = db.one(
        """SELECT * FROM sms_reminder_intents
           WHERE appointment_id=? AND reminder_date=?""",
        (appointment_id, reminder_date),
    )
    assert row["status"] == "failed"
    assert row["claimed_at"] is None
    assert "unknown" in row["error"].lower()
    assert "verify" in row["error"].lower()

    assert sms.shoot_day_reminders() == 0
    send.assert_called_once()


@pytest.mark.parametrize("old_outcome", ["success", "failure"])
def test_delivery_stale_worker_cannot_overwrite_new_retry(outbound_env, monkeypatch, old_outcome):
    del outbound_env
    notification_id = _add_delivery_notification()
    attempt_tokens: list[str] = []

    def send(*_args, **_kwargs):
        row = _row("delivery_notifications", notification_id)
        attempt_tokens.append(row["attempt_token"])
        if len(attempt_tokens) == 1:
            db.run(
                """UPDATE delivery_notifications
                   SET claimed_at='2000-01-01 00:00:00' WHERE id=?""",
                (notification_id,),
            )
            assert delivery_notify._fail_stale_claims() == 1
            delivery_notify.reconcile_notification(notification_id, delivered=False)
            assert delivery_notify.retry_notification(notification_id) is True
            assert delivery_notify.process_notification(notification_id) is True
            if old_outcome == "failure":
                raise RuntimeError("old delivery worker failed late")

    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send_for_studio", send)

    assert delivery_notify.process_notification(notification_id) is False
    row = _row("delivery_notifications", notification_id)
    assert row["status"] == "sent"
    assert row["attempts"] == 2
    assert row["claimed_at"] is None
    assert row["attempt_token"] is None
    assert row["error"] is None
    assert len(set(attempt_tokens)) == 2
    assert (
        db.one(
            """SELECT COUNT(*) AS n FROM emails_log
               WHERE doc_kind='gallery_delivery' AND doc_id=?""",
            (notification_id,),
        )["n"]
        == 1
    )


@pytest.mark.parametrize("old_outcome", ["success", "failure"])
def test_sequence_stale_worker_cannot_overwrite_new_retry(outbound_env, monkeypatch, old_outcome):
    del outbound_env
    run_id = _add_sequence_run()
    attempt_tokens: list[str] = []

    def send(*_args, **_kwargs):
        row = _row("email_sequence_runs", run_id)
        attempt_tokens.append(row["attempt_token"])
        if len(attempt_tokens) == 1:
            db.run(
                """UPDATE email_sequence_runs
                   SET claimed_at='2000-01-01 00:00:00' WHERE id=?""",
                (run_id,),
            )
            assert sequences._fail_stale_claims() == 1
            sequences.reconcile_run(run_id, delivered=False)
            assert sequences.retry_run(run_id) is True
            assert sequences.process_due() == 1
            if old_outcome == "failure":
                raise RuntimeError("old sequence worker failed late")

    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send_for_studio", send)

    assert sequences.process_due() == 0
    row = _row("email_sequence_runs", run_id)
    assert row["status"] == "sent"
    assert row["attempts"] == 2
    assert row["claimed_at"] is None
    assert row["attempt_token"] is None
    assert row["error"] is None
    assert len(set(attempt_tokens)) == 2
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM emails_log WHERE doc_kind='sequence' AND doc_id=?",
            (run_id,),
        )["n"]
        == 1
    )


@pytest.mark.parametrize("old_outcome", ["success", "failure"])
def test_sms_stale_worker_cannot_overwrite_new_retry(outbound_env, monkeypatch, old_outcome):
    del outbound_env
    reminder_date = "2026-08-06"
    appointment_id = _add_appointment("default", reminder_date, phone="+15550005555")
    attempt_tokens: list[str] = []

    def send(*, to_phone: str, body: str) -> bool:
        assert to_phone and body
        row = db.one(
            """SELECT * FROM sms_reminder_intents
               WHERE appointment_id=? AND reminder_date=?""",
            (appointment_id, reminder_date),
        )
        attempt_tokens.append(row["attempt_token"])
        if len(attempt_tokens) == 1:
            db.run(
                """UPDATE sms_reminder_intents
                   SET claimed_at='2000-01-01 00:00:00' WHERE id=?""",
                (row["id"],),
            )
            assert sms._fail_stale_claims() == 1
            sms.reconcile_reminder(row["id"], delivered=False)
            assert sms.retry_reminder(row["id"]) is True
            assert sms.shoot_day_reminders() == 1
            if old_outcome == "failure":
                raise RuntimeError("old SMS worker failed late")
        return True

    monkeypatch.setattr(sms, "configured", lambda: True)
    monkeypatch.setattr(sms, "_local_date", lambda: reminder_date)
    monkeypatch.setattr(sms, "send", send)

    assert sms.shoot_day_reminders() == 0
    row = db.one(
        """SELECT * FROM sms_reminder_intents
           WHERE appointment_id=? AND reminder_date=?""",
        (appointment_id, reminder_date),
    )
    assert row["status"] == "sent"
    assert row["attempts"] == 2
    assert row["claimed_at"] is None
    assert row["attempt_token"] is None
    assert row["error"] is None
    assert len(set(attempt_tokens)) == 2


def test_local_reminder_date_uses_configured_timezone(outbound_env, monkeypatch):
    del outbound_env
    real_datetime = dt.datetime

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            instant = real_datetime(2026, 7, 26, 3, 30, tzinfo=dt.UTC)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    monkeypatch.setattr(sms, "dt", SimpleNamespace(datetime=FrozenDateTime))

    assert sms._local_date() == "2026-07-25"


def test_twilio_transport_timeout_is_unknown_and_not_logged_as_definite_failure(
    outbound_env, monkeypatch
):
    del outbound_env
    monkeypatch.setattr(sms.config, "TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setattr(sms.config, "TWILIO_AUTH_TOKEN", "twilio-test-token")
    monkeypatch.setattr(sms.config, "TWILIO_FROM_NUMBER", "+15550001111")
    monkeypatch.setattr(
        sms.httpx,
        "post",
        Mock(side_effect=TimeoutError("response timed out after dispatch")),
    )

    with pytest.raises(sms.SmsOutcomeUnknown, match="verify Twilio"):
        sms.send(to_phone="+15550002222", body="Shoot reminder")

    assert db.one("SELECT COUNT(*) AS n FROM sms_log")["n"] == 0


def test_twilio_confirmed_rejection_is_a_definite_failure(outbound_env, monkeypatch):
    del outbound_env
    monkeypatch.setattr(sms.config, "TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setattr(sms.config, "TWILIO_AUTH_TOKEN", "twilio-test-token")
    monkeypatch.setattr(sms.config, "TWILIO_FROM_NUMBER", "+15550001111")

    class RejectedResponse:
        def raise_for_status(self):
            raise RuntimeError("HTTP 400")

    monkeypatch.setattr(sms.httpx, "post", Mock(return_value=RejectedResponse()))

    assert sms.send(to_phone="+15550002222", body="Shoot reminder") is False
    log_row = db.one("SELECT status FROM sms_log ORDER BY id DESC LIMIT 1")
    assert log_row["status"] == "failed"


def test_mailer_marks_ambiguous_transports_unknown(outbound_env, monkeypatch):
    del outbound_env

    class AmbiguousSmtp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, *_args):
            return None

        def send_message(self, _message):
            raise TimeoutError("SMTP response timed out")

    monkeypatch.setattr(config, "EMAIL_PROVIDER", "smtp")
    monkeypatch.setattr(config, "GMAIL_USER", "sender@example.test")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", lambda *_args, **_kwargs: AmbiguousSmtp())

    with pytest.raises(mailer.DeliveryOutcomeUnknown, match="SMTP dispatch"):
        mailer.send("agent@example.test", "Subject", "Body")

    monkeypatch.setattr(config, "EMAIL_PROVIDER", "postmark")
    monkeypatch.setattr(config, "POSTMARK_API_KEY", "test-key")
    monkeypatch.setattr(config, "POSTMARK_FROM_EMAIL", "sender@example.test")

    def timeout(*_args, **_kwargs):
        raise TimeoutError("Postmark response timed out")

    monkeypatch.setattr(mailer.httpx, "post", timeout)
    with pytest.raises(mailer.DeliveryOutcomeUnknown, match="Postmark dispatch"):
        mailer.send("agent@example.test", "Subject", "Body")


def test_mailer_keeps_definitive_postmark_rejection_retryable(outbound_env, monkeypatch):
    del outbound_env
    response = SimpleNamespace(status_code=503, text="temporarily unavailable")
    monkeypatch.setattr(config, "EMAIL_PROVIDER", "postmark")
    monkeypatch.setattr(config, "POSTMARK_API_KEY", "test-key")
    monkeypatch.setattr(config, "POSTMARK_FROM_EMAIL", "sender@example.test")
    monkeypatch.setattr(mailer.httpx, "post", lambda *_args, **_kwargs: response)

    with pytest.raises(RuntimeError, match="Postmark send failed") as exc:
        mailer.send("agent@example.test", "Subject", "Body")
    assert not isinstance(exc.value, mailer.DeliveryOutcomeUnknown)


def test_mailer_does_not_treat_postmark_redirect_as_acceptance(outbound_env, monkeypatch):
    del outbound_env
    response = SimpleNamespace(status_code=302, text="redirected")
    monkeypatch.setattr(config, "EMAIL_PROVIDER", "postmark")
    monkeypatch.setattr(config, "POSTMARK_API_KEY", "test-key")
    monkeypatch.setattr(config, "POSTMARK_FROM_EMAIL", "sender@example.test")
    monkeypatch.setattr(mailer.httpx, "post", lambda *_args, **_kwargs: response)

    with pytest.raises(RuntimeError, match=r"Postmark send failed \(302\)"):
        mailer.send("agent@example.test", "Subject", "Body")
