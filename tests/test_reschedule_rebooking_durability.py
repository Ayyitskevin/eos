"""Race and recovery regressions for rescheduling and rebooking outreach."""

from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from unittest.mock import Mock

import pytest
from eos import clients, db, rebooking, reschedule, scheduling, tenant
from fastapi import HTTPException


def _seed_reschedule_holds(count: int) -> tuple[str, list[tuple[int, int, str]]]:
    tenant.set_studio("default")
    slots = scheduling.reschedule_slots()
    assert slots, "test fixture requires at least one reschedule slot"
    target = slots[0]["value"]
    holds: list[tuple[int, int, str]] = []
    for index in range(count):
        client_id = db.run(
            """INSERT INTO clients (studio_id, name, email)
               VALUES ('default', ?, ?)""",
            (f"Race Agent {index}", f"race-{index}@example.test"),
        )
        appointment_id = db.run(
            """INSERT INTO appointments
               (studio_id, client_id, title, status, starts_at, token)
               VALUES ('default', ?, ?, 'proposed', NULL, ?)""",
            (client_id, f"Race Shoot {index}", f"race-appointment-{index}"),
        )
        hold_token = f"race-hold-{index}"
        db.run(
            """INSERT INTO appointment_holds
               (studio_id, appointment_id, client_id, starts_at, token, expires_at)
               VALUES ('default', ?, ?, ?, ?, datetime('now', '+15 minutes'))""",
            (appointment_id, client_id, target, hold_token),
        )
        holds.append((appointment_id, client_id, hold_token))
    return target, holds


def _seed_rebooking_opportunity(label: str) -> int:
    tenant.set_studio("default")
    client_id = clients.create_client(
        f"{label} Agent",
        email=f"{label.lower().replace(' ', '-')}@example.test",
        client_type="agent",
    )
    db.run(
        """INSERT INTO listings
           (studio_id, client_id, title, status, created_at, delivered_at)
           VALUES ('default', ?, ?, 'delivered',
                   datetime('now', '-120 days'), datetime('now', '-120 days'))""",
        (client_id, f"{label} old listing"),
    )
    return client_id


def _unknown_rebooking_intent(
    client_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    monkeypatch.setattr(rebooking.mailer, "configured", lambda: True)
    monkeypatch.setattr(
        rebooking.mailer,
        "send_for_studio",
        Mock(side_effect=rebooking.mailer.DeliveryOutcomeUnknown("provider timeout")),
    )

    with pytest.raises(HTTPException) as exc_info:
        rebooking.send_email(client_id)

    assert exc_info.value.status_code == 502
    intent = db.one(
        """SELECT id, status FROM rebooking_email_intents
           WHERE studio_id='default' AND client_id=?""",
        (client_id,),
    )
    assert intent["status"] == "unknown"
    return intent["id"]


def test_concurrent_reschedule_confirms_exactly_one_and_consumed_hold_is_not_replayable(
    app_env_http,
):
    target, holds = _seed_reschedule_holds(2)
    barrier = threading.Barrier(2)

    def confirm(item: tuple[int, int, str]) -> tuple[str, int, int]:
        appointment_id, client_id, token = item
        tenant.set_studio("default")
        barrier.wait(timeout=5)
        try:
            confirmed_id = reschedule.confirm_hold(token, client_id=client_id)
        except HTTPException as exc:
            return token, exc.status_code, appointment_id
        return token, 200, confirmed_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(confirm, holds))

    assert sorted(outcome[1] for outcome in outcomes) == [200, 409]
    winner_token, _, winner_id = next(outcome for outcome in outcomes if outcome[1] == 200)
    loser_id = next(outcome[2] for outcome in outcomes if outcome[1] == 409)

    booked = db.all_(
        """SELECT id, status, starts_at FROM appointments
           WHERE studio_id='default' AND starts_at=?""",
        (target,),
    )
    assert [row["id"] for row in booked] == [winner_id]
    assert booked[0]["status"] == "confirmed"
    loser = db.one(
        "SELECT status, starts_at FROM appointments WHERE id=? AND studio_id='default'",
        (loser_id,),
    )
    assert dict(loser) == {"status": "proposed", "starts_at": None}
    remaining_hold = db.one(
        """SELECT appointment_id FROM appointment_holds
           WHERE studio_id='default'"""
    )
    assert remaining_hold["appointment_id"] == loser_id

    winner_client_id = next(item[1] for item in holds if item[0] == winner_id)
    with pytest.raises(HTTPException) as replay:
        reschedule.confirm_hold(winner_token, client_id=winner_client_id)
    assert replay.value.status_code == 404


def test_reschedule_confirmation_rolls_back_appointment_and_hold_on_failure(
    app_env_http,
    monkeypatch,
):
    _target, holds = _seed_reschedule_holds(1)
    appointment_id, client_id, hold_token = holds[0]
    before = db.one(
        """SELECT status, starts_at, ends_at FROM appointments
           WHERE id=? AND studio_id='default'""",
        (appointment_id,),
    )
    original_run = db.run

    def fail_before_hold_consumption(sql: str, params: tuple = ()) -> int:
        if sql.lstrip().startswith("DELETE FROM appointment_holds"):
            raise RuntimeError("injected hold-consumption failure")
        return original_run(sql, params)

    monkeypatch.setattr(reschedule.db, "run", fail_before_hold_consumption)

    with pytest.raises(RuntimeError, match="injected hold-consumption failure"):
        reschedule.confirm_hold(hold_token, client_id=client_id)

    after = db.one(
        """SELECT status, starts_at, ends_at FROM appointments
           WHERE id=? AND studio_id='default'""",
        (appointment_id,),
    )
    assert dict(after) == dict(before)
    assert (
        db.one(
            """SELECT COUNT(*) AS n FROM appointment_holds
               WHERE token=? AND appointment_id=? AND studio_id='default'""",
            (hold_token, appointment_id),
        )["n"]
        == 1
    )
    assert (
        db.one(
            """SELECT COUNT(*) AS n FROM audit_log
               WHERE studio_id='default'
                 AND action IN ('appointment.update', 'appointment.reschedule')"""
        )["n"]
        == 0
    )


def test_concurrent_rebooking_send_claims_provider_once(app_env_http, monkeypatch):
    client_id = _seed_rebooking_opportunity("Concurrent")
    monkeypatch.setattr(rebooking.mailer, "configured", lambda: True)
    provider_entered = threading.Event()
    release_provider = threading.Event()
    calls_lock = threading.Lock()
    provider_calls = 0

    def provider_send(_to: str, _subject: str, _body: str) -> None:
        nonlocal provider_calls
        with calls_lock:
            provider_calls += 1
        provider_entered.set()
        assert release_provider.wait(timeout=5)

    monkeypatch.setattr(rebooking.mailer, "send_for_studio", provider_send)
    barrier = threading.Barrier(2)

    def send() -> dict:
        tenant.set_studio("default")
        barrier.wait(timeout=5)
        return rebooking.send_email(client_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(send) for _ in range(2)]
        try:
            assert provider_entered.wait(timeout=5)
            completed, _pending = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
            assert len(completed) == 1
        finally:
            release_provider.set()
        results = [future.result(timeout=5) for future in futures]

    assert sorted(result["status"] for result in results) == ["review", "sent"]
    assert provider_calls == 1
    intent = db.one(
        """SELECT status, attempts FROM rebooking_email_intents
           WHERE studio_id='default' AND client_id=?""",
        (client_id,),
    )
    assert dict(intent) == {"status": "sent", "attempts": 1}


def test_unknown_rebooking_outcome_blocks_automatic_replay(app_env_http, monkeypatch):
    client_id = _seed_rebooking_opportunity("Unknown")
    intent_id = _unknown_rebooking_intent(client_id, monkeypatch)
    replay_provider = Mock()
    monkeypatch.setattr(rebooking.mailer, "send_for_studio", replay_provider)

    replay = rebooking.send_email(client_id)

    assert replay["status"] == "review"
    assert replay["intent"]["id"] == intent_id
    assert replay["intent"]["status"] == "unknown"
    replay_provider.assert_not_called()


def test_provider_success_with_local_commit_failure_stays_closed_to_replay(
    app_env_http,
    monkeypatch,
):
    client_id = _seed_rebooking_opportunity("Commit Failure")
    provider = Mock()
    monkeypatch.setattr(rebooking.mailer, "configured", lambda: True)
    monkeypatch.setattr(rebooking.mailer, "send_for_studio", provider)
    monkeypatch.setattr(
        rebooking,
        "_mark_intent_sent",
        Mock(side_effect=RuntimeError("injected local commit failure")),
    )

    with pytest.raises(HTTPException) as exc_info:
        rebooking.send_email(client_id)

    assert exc_info.value.status_code == 502
    intent = db.one(
        """SELECT id, status, error FROM rebooking_email_intents
           WHERE studio_id='default' AND client_id=?""",
        (client_id,),
    )
    assert intent["status"] in {"claimed", "unknown"}
    assert "injected local commit failure" in (intent["error"] or "")

    replay = rebooking.send_email(client_id)
    assert replay["status"] == "review"
    assert replay["intent"]["id"] == intent["id"]
    provider.assert_called_once()


def test_operator_reconcile_delivered_records_send_and_keeps_replay_closed(
    app_env_http,
    monkeypatch,
):
    client_id = _seed_rebooking_opportunity("Delivered Reconcile")
    intent_id = _unknown_rebooking_intent(client_id, monkeypatch)

    rebooking.reconcile_intent(intent_id, client_id=client_id, delivered=True)

    intent = db.one(
        "SELECT status, sent_at, error FROM rebooking_email_intents WHERE id=?",
        (intent_id,),
    )
    assert intent["status"] == "sent"
    assert intent["sent_at"] is not None
    assert intent["error"] is None
    assert (
        db.one(
            """SELECT COUNT(*) AS n FROM emails_log
               WHERE studio_id='default' AND doc_kind='rebooking' AND doc_id=?""",
            (client_id,),
        )["n"]
        == 1
    )
    assert rebooking.send_email(client_id)["status"] == "cooldown"

    with pytest.raises(HTTPException) as replay:
        rebooking.reconcile_intent(intent_id, client_id=client_id, delivered=True)
    assert replay.value.status_code == 404


def test_operator_reconcile_not_delivered_releases_one_deliberate_retry(
    app_env_http,
    monkeypatch,
):
    client_id = _seed_rebooking_opportunity("Not Delivered Reconcile")
    intent_id = _unknown_rebooking_intent(client_id, monkeypatch)

    rebooking.reconcile_intent(intent_id, client_id=client_id, delivered=False)

    failed = db.one(
        "SELECT status, error FROM rebooking_email_intents WHERE id=?",
        (intent_id,),
    )
    assert dict(failed) == {
        "status": "failed",
        "error": "Operator confirmed provider did not deliver",
    }
    retry_provider = Mock()
    monkeypatch.setattr(rebooking.mailer, "send_for_studio", retry_provider)

    retry = rebooking.send_email(client_id)

    assert retry["status"] == "sent"
    retry_provider.assert_called_once()
    retried = db.one(
        "SELECT status, attempts, sent_at FROM rebooking_email_intents WHERE id=?",
        (intent_id,),
    )
    assert retried["status"] == "sent"
    assert retried["attempts"] == 2
    assert retried["sent_at"] is not None


def test_stale_rebooking_worker_cannot_finalize_a_newer_retry(app_env_http):
    client_id = _seed_rebooking_opportunity("Attempt Fence")
    draft = rebooking.build_email(client_id)
    first = rebooking._claim_email_intent(
        draft,
        cooldown_days=rebooking.COOLDOWN_DAYS,
    )
    intent_id = first["intent_id"]
    first_token = first["claim_token"]
    db.run(
        """UPDATE rebooking_email_intents
           SET claimed_at=datetime('now','-11 minutes') WHERE id=?""",
        (intent_id,),
    )
    rebooking.reconcile_intent(intent_id, client_id=client_id, delivered=False)

    retry = rebooking._claim_email_intent(
        draft,
        cooldown_days=rebooking.COOLDOWN_DAYS,
    )
    assert retry["intent_id"] == intent_id
    assert retry["claim_token"] != first_token

    with pytest.raises(RuntimeError, match="no longer active"):
        rebooking._mark_intent_sent(intent_id, first_token)
    rebooking._mark_intent_failed(
        intent_id,
        first_token,
        RuntimeError("late stale failure"),
        outcome_unknown=True,
    )
    active = db.one(
        """SELECT status, attempts, claim_token, error
           FROM rebooking_email_intents WHERE id=?""",
        (intent_id,),
    )
    assert dict(active) == {
        "status": "claimed",
        "attempts": 2,
        "claim_token": retry["claim_token"],
        "error": None,
    }

    rebooking._mark_intent_sent(intent_id, retry["claim_token"])
    assert (
        db.one("SELECT status FROM rebooking_email_intents WHERE id=?", (intent_id,))["status"]
        == "sent"
    )


def test_rebooking_reconciliation_is_tenant_scoped(app_env_http):
    tenant.set_studio("default")
    db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other Studio', 'other')")
    other_client_id = db.run(
        """INSERT INTO clients (studio_id, name, email, client_type)
           VALUES ('other', 'Other Agent', 'other@example.test', 'agent')"""
    )
    intent_id = db.run(
        """INSERT INTO rebooking_email_intents
           (studio_id, client_id, event_key, to_email, subject, status, error,
            claim_token)
           VALUES ('other', ?, 'rebooking:other:first', 'other@example.test',
                   'Other subject', 'unknown', 'provider outcome unknown',
                   'other-claim')""",
        (other_client_id,),
    )

    with pytest.raises(HTTPException) as exc_info:
        rebooking.reconcile_intent(
            intent_id,
            client_id=other_client_id,
            delivered=True,
        )

    assert exc_info.value.status_code == 404
    assert rebooking.unresolved_intent(other_client_id) is None
    row = db.one(
        "SELECT studio_id, status FROM rebooking_email_intents WHERE id=?",
        (intent_id,),
    )
    assert dict(row) == {"studio_id": "other", "status": "unknown"}
