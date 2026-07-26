"""Tenant-owned timezone behavior across booking and outbound integrations."""

from __future__ import annotations

import datetime as dt
import importlib
from types import SimpleNamespace

import eos.config as config
import eos.db as db
import eos.scheduling as scheduling
import eos.sms as sms
import eos.studio as studio
import eos.tenant as tenant
import pytest


@pytest.fixture()
def timezone_env(tmp_path, monkeypatch):
    values = {
        "EOS_DATA_DIR": str(tmp_path / "data"),
        "EOS_SECRET_KEY": "test-secret-key-32chars-minimum!!",
        "EOS_ADMIN_PASSWORD": "test-admin-pass",
        "EOS_BASE_URL": "http://eos.test",
        "EOS_BASE_DOMAIN": "eos.test",
        "EOS_TIMEZONE": "America/Los_Angeles",
        "EOS_SAAS_MODE": "false",
        "EOS_SIGNUP_ENABLED": "false",
        "EOS_BILLING_ENFORCE": "false",
        "EOS_DEMO_ENABLED": "false",
        "EOS_TWILIO_ACCOUNT_SID": "",
        "EOS_TWILIO_AUTH_TOKEN": "",
        "EOS_TWILIO_FROM_NUMBER": "",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for module in (config, db, tenant, studio, scheduling, sms):
        importlib.reload(module)
    from eos.integrations import google_calendar

    importlib.reload(google_calendar)
    config.ensure_dirs()
    db.migrate()
    tenant.set_studio("default")
    yield SimpleNamespace(google=google_calendar)
    tenant.set_studio("default")


def _add_studio(
    studio_id: str,
    timezone: str,
    *,
    booking_window: tuple[int, int, int] = (1320, 1380, 60),
) -> None:
    db.run(
        "INSERT INTO studio (id, name, slug, timezone) VALUES (?,?,?,?)",
        (studio_id, f"{studio_id.title()} Photo", studio_id, timezone),
    )
    day_start, day_end, slot_minutes = booking_window
    db.run(
        """INSERT INTO studio_profiles
           (studio_id, booking_enabled, min_notice_hours, buffer_minutes,
            slot_minutes, day_start_min, day_end_min, book_weekdays)
           VALUES (?,1,1,0,?,?,?,'0,1,2,3,4,5,6')""",
        (studio_id, slot_minutes, day_start, day_end),
    )


def _add_appointment(studio_id: str, starts_at: str, *, phone: str = "") -> int:
    client_id = db.run(
        """INSERT INTO clients (studio_id, name, email, phone)
           VALUES (?,?,?,?)""",
        (
            studio_id,
            f"{studio_id.title()} Agent",
            f"agent-{studio_id}-{starts_at[:10]}@example.test",
            phone,
        ),
    )
    return db.run(
        """INSERT INTO appointments
           (studio_id, client_id, title, kind, status, starts_at, token)
           VALUES (?,?,?,'shoot','confirmed',?,?)""",
        (
            studio_id,
            client_id,
            f"{studio_id.title()} Shoot",
            starts_at,
            f"{studio_id}-{starts_at}-shoot",
        ),
    )


def test_booking_slots_use_active_studio_local_date_without_bleed(timezone_env, monkeypatch):
    del timezone_env
    _add_studio("alpha", "America/New_York")
    _add_studio("beta", "Asia/Tokyo")
    instant = dt.datetime(2026, 7, 26, 0, 30, tzinfo=dt.UTC)
    monkeypatch.setattr(scheduling, "_now", lambda zone: instant.astimezone(zone))

    tenant.set_studio("alpha")
    alpha = scheduling.open_slots(days=1)
    tenant.set_studio("beta")
    beta = scheduling.open_slots(days=1)

    assert [slot["value"] for slot in alpha] == ["2026-07-25 22:00:00"]
    assert [slot["value"] for slot in beta] == ["2026-07-26 22:00:00"]
    assert scheduling._studio_zone().key == "Asia/Tokyo"


def test_booking_slots_fail_closed_and_repeat_deterministically_across_dst_gap(
    timezone_env, monkeypatch
):
    del timezone_env
    _add_studio(
        "dst",
        "America/New_York",
        booking_window=(60, 240, 30),
    )
    tenant.set_studio("dst")
    instant = dt.datetime(2026, 3, 8, 5, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(scheduling, "_now", lambda zone: instant.astimezone(zone))

    first = scheduling.open_slots(days=1)
    second = scheduling.open_slots(days=1)

    assert first == second
    values = [slot["value"] for slot in first]
    assert values == [
        "2026-03-08 01:00:00",
        "2026-03-08 03:00:00",
        "2026-03-08 03:30:00",
    ]
    assert all(" 02:" not in value for value in values)


def test_sms_sweep_uses_each_tenant_date_and_replay_sends_once(timezone_env, monkeypatch):
    del timezone_env
    _add_studio("alpha", "America/New_York")
    _add_studio("beta", "Asia/Tokyo")
    _add_appointment("alpha", "2026-07-25 10:00:00", phone="+15550001111")
    _add_appointment("alpha", "2026-07-26 10:00:00", phone="+15550001112")
    _add_appointment("beta", "2026-07-25 10:00:00", phone="+15550002221")
    _add_appointment("beta", "2026-07-26 10:00:00", phone="+15550002222")
    instant = dt.datetime(2026, 7, 26, 0, 30, tzinfo=dt.UTC)
    monkeypatch.setattr(sms, "_now", lambda zone: instant.astimezone(zone))
    monkeypatch.setattr(sms, "configured", lambda: True)
    sends: list[tuple[str, str]] = []

    def record_send(*, to_phone: str, body: str) -> bool:
        assert body
        sends.append((tenant.get_studio_id(), to_phone))
        return True

    monkeypatch.setattr(sms, "send", record_send)
    tenant.set_studio("default")

    assert sms.shoot_day_reminders() == 2
    assert sms.shoot_day_reminders() == 0
    assert tenant.get_studio_id() == "default"
    assert sends == [
        ("alpha", "+15550001111"),
        ("beta", "+15550002222"),
    ]
    intents = db.all_(
        """SELECT studio_id, reminder_date, status, attempts
           FROM sms_reminder_intents ORDER BY studio_id"""
    )
    assert [tuple(row) for row in intents] == [
        ("alpha", "2026-07-25", "sent", 1),
        ("beta", "2026-07-26", "sent", 1),
    ]


class _GoogleResponse:
    def __init__(self, data: dict):
        self._data = data
        self.status_code = 200

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        return None


def test_google_payload_and_busy_ranges_are_tenant_local(timezone_env, monkeypatch):
    google = timezone_env.google
    _add_studio("alpha", "America/New_York")
    _add_studio("beta", "Asia/Tokyo")
    alpha_appt = _add_appointment("alpha", "2026-07-27 10:00:00")
    beta_appt = _add_appointment("beta", "2026-07-27 10:00:00")
    instant = dt.datetime(2026, 7, 26, 0, 30, tzinfo=dt.UTC)
    monkeypatch.setattr(google, "is_intended_enabled", lambda: True)
    monkeypatch.setattr(google, "is_connected", lambda: True)
    monkeypatch.setattr(google, "_token", lambda: "google-token")
    monkeypatch.setattr(google, "_calendar_id", lambda: "primary")
    monkeypatch.setattr(google, "_now", lambda zone: instant.astimezone(zone))
    requests: list[dict] = []

    def free_busy(_url, **kwargs):
        requests.append(kwargs["json"])
        return _GoogleResponse(
            {
                "calendars": {
                    "primary": {
                        "busy": [
                            {
                                "start": "2026-07-26T00:00:00Z",
                                "end": "2026-07-26T01:00:00Z",
                            }
                        ]
                    }
                }
            }
        )

    monkeypatch.setattr(google.httpx, "post", free_busy)

    tenant.set_studio("alpha")
    alpha_body = google._event_body(db.one("SELECT * FROM appointments WHERE id=?", (alpha_appt,)))
    alpha_busy = google.busy_ranges()
    tenant.set_studio("beta")
    beta_body = google._event_body(db.one("SELECT * FROM appointments WHERE id=?", (beta_appt,)))
    beta_busy = google.busy_ranges()

    assert alpha_body["start"] == {
        "dateTime": "2026-07-27T10:00:00",
        "timeZone": "America/New_York",
    }
    assert beta_body["start"] == {
        "dateTime": "2026-07-27T10:00:00",
        "timeZone": "Asia/Tokyo",
    }
    assert alpha_busy == [(dt.datetime(2026, 7, 25, 20), dt.datetime(2026, 7, 25, 21))]
    assert beta_busy == [(dt.datetime(2026, 7, 26, 9), dt.datetime(2026, 7, 26, 10))]
    assert [(body["timeZone"], body["timeMin"][-6:]) for body in requests] == [
        ("America/New_York", "-04:00"),
        ("Asia/Tokyo", "+09:00"),
    ]


def test_google_fall_back_busy_range_never_collapses_to_zero(timezone_env, monkeypatch):
    google = timezone_env.google
    db.run("UPDATE studio SET timezone='America/New_York' WHERE id='default'")
    monkeypatch.setattr(google, "is_intended_enabled", lambda: True)
    monkeypatch.setattr(google, "is_connected", lambda: True)
    monkeypatch.setattr(google, "_token", lambda: "google-token")
    monkeypatch.setattr(google, "_calendar_id", lambda: "primary")
    monkeypatch.setattr(
        google.httpx,
        "post",
        lambda *_args, **_kwargs: _GoogleResponse(
            {
                "calendars": {
                    "primary": {
                        "busy": [
                            {
                                "start": "2026-11-01T05:30:00Z",
                                "end": "2026-11-01T06:30:00Z",
                            }
                        ]
                    }
                }
            }
        ),
    )

    assert google.busy_ranges() == [
        (dt.datetime(2026, 11, 1, 1, 30), dt.datetime(2026, 11, 1, 2, 30))
    ]


def test_google_freebusy_outage_closes_public_slots(timezone_env, monkeypatch):
    google = timezone_env.google
    db.run("UPDATE studio_profiles SET booking_enabled=1 WHERE studio_id='default'")
    monkeypatch.setattr(google, "is_intended_enabled", lambda: True)
    monkeypatch.setattr(google, "is_connected", lambda: True)
    monkeypatch.setattr(google, "_token", lambda: "google-token")
    monkeypatch.setattr(
        google.httpx,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("provider timeout")),
    )

    with pytest.raises(google.GoogleAvailabilityUnavailable):
        google.busy_ranges()
    assert scheduling.open_slots(days=1) == []
    assert scheduling.slot_is_open("2026-07-27 10:00:00") is False


def test_google_enabled_without_connection_closes_public_slots(timezone_env):
    google = timezone_env.google
    studio.get_profile()
    db.run(
        """UPDATE studio_profiles
           SET booking_enabled=1, google_calendar_enabled=1
           WHERE studio_id='default'"""
    )
    db.run("DELETE FROM studio_oauth WHERE studio_id='default' AND provider='google'")

    with pytest.raises(google.GoogleAvailabilityUnavailable):
        google.busy_ranges()
    assert scheduling.open_slots(days=1) == []
    assert scheduling.slot_is_open("2026-07-27 10:00:00") is False


def test_google_pull_normalizes_provider_offset_to_studio_wall_time(timezone_env, monkeypatch):
    google = timezone_env.google
    _add_studio("alpha", "America/New_York")
    tenant.set_studio("alpha")
    monkeypatch.setattr(google, "is_intended_enabled", lambda: True)
    monkeypatch.setattr(google, "_token", lambda: "google-token")
    monkeypatch.setattr(google, "_calendar_id", lambda: "primary")
    monkeypatch.setattr(google.oauth_store, "get_connection", lambda _provider: None)
    monkeypatch.setattr(
        google.httpx,
        "get",
        lambda *_args, **_kwargs: _GoogleResponse(
            {
                "items": [
                    {
                        "id": "provider-alpha-event",
                        "summary": "UTC provider event",
                        "start": {"dateTime": "2026-07-26T00:30:00Z"},
                        "end": {"dateTime": "2026-07-26T01:30:00Z"},
                    }
                ]
            }
        ),
    )

    assert google.pull_changes() == 1
    appointment = db.one(
        """SELECT starts_at, ends_at FROM appointments
           WHERE studio_id='alpha' AND google_event_id='provider-alpha-event'"""
    )
    assert tuple(appointment) == ("2026-07-25 20:30:00", "2026-07-25 21:30:00")


def test_google_sweep_restores_calling_tenant(timezone_env, monkeypatch):
    google = timezone_env.google
    _add_studio("alpha", "America/New_York")
    _add_studio("beta", "Asia/Tokyo")
    seen: list[str] = []
    monkeypatch.setattr(
        google.db,
        "all_",
        lambda _sql, _params=(): [{"studio_id": "alpha"}, {"studio_id": "beta"}],
    )
    monkeypatch.setattr(
        google,
        "pull_changes",
        lambda: seen.append(tenant.get_studio_id()) or 1,
    )
    tenant.set_studio("default")

    assert google.sweep_all() == 2
    assert seen == ["alpha", "beta"]
    assert tenant.get_studio_id() == "default"


def test_invalid_studio_timezone_uses_validated_operator_fallback(timezone_env):
    google = timezone_env.google
    db.run("UPDATE studio SET timezone='Mars/Olympus_Mons' WHERE id='default'")

    assert scheduling._studio_zone().key == "America/Los_Angeles"
    assert sms._studio_zone().key == "America/Los_Angeles"
    assert google._studio_zone().key == "America/Los_Angeles"
