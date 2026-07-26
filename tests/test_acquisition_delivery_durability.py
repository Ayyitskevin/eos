"""Durable referral-outreach claims, replay safety, and operator recovery."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from eos import acquisition, clients, db, tenant
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient


def _seed_agent(label: str = "Durable") -> int:
    tenant.set_studio("default")
    return clients.create_client(
        f"{label} Agent",
        email=f"{label.lower()}@example.test",
        client_type="agent",
    )


def test_concurrent_intro_send_claims_exactly_one_provider_attempt(app_env, monkeypatch):
    client_id = _seed_agent("Concurrent")
    send_calls: list[str] = []
    call_lock = threading.Lock()
    start = threading.Barrier(2)
    monkeypatch.setattr(acquisition.mailer, "configured", lambda: True)

    def provider_send(to: str, _subject: str, _body: str) -> None:
        with call_lock:
            send_calls.append(to)

    monkeypatch.setattr(acquisition.mailer, "send_for_studio", provider_send)

    def send() -> str:
        tenant.set_studio("default")
        start.wait(timeout=5)
        return acquisition.send_intro_email(client_id)["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: send(), range(2)))

    assert statuses.count("sent") == 1
    assert set(statuses) <= {"sent", "review", "cooldown"}
    assert send_calls == ["concurrent@example.test"]
    intent = db.one(
        """SELECT status, attempts FROM acquisition_email_intents
           WHERE studio_id='default' AND client_id=? AND email_kind='intro'""",
        (client_id,),
    )
    assert dict(intent) == {"status": "sent", "attempts": 1}


def test_unknown_intro_outcome_blocks_replay_until_provider_reconciliation(app_env, monkeypatch):
    client_id = _seed_agent("Unknown")
    attempts = 0
    monkeypatch.setattr(acquisition.mailer, "configured", lambda: True)

    def provider_send(_to: str, _subject: str, _body: str) -> None:
        nonlocal attempts
        attempts += 1
        raise acquisition.mailer.DeliveryOutcomeUnknown("provider timeout")

    monkeypatch.setattr(acquisition.mailer, "send_for_studio", provider_send)

    with pytest.raises(HTTPException) as send_error:
        acquisition.send_intro_email(client_id)
    assert send_error.value.status_code == 502
    replay = acquisition.send_intro_email(client_id)

    assert replay["status"] == "review"
    assert attempts == 1
    intent = db.one(
        """SELECT id, status, attempts FROM acquisition_email_intents
           WHERE studio_id='default' AND client_id=? AND email_kind='intro'""",
        (client_id,),
    )
    assert dict(intent) == {"id": intent["id"], "status": "unknown", "attempts": 1}
    assert [row["id"] for row in acquisition.unresolved_email_intents()] == [intent["id"]]

    acquisition.reconcile_email_intent(intent["id"], client_id=client_id, delivered=True)
    assert acquisition.send_intro_email(client_id)["status"] == "cooldown"
    assert attempts == 1
    assert db.one(
        """SELECT 1 FROM emails_log
           WHERE studio_id='default' AND doc_kind='acquisition_intro' AND doc_id=?""",
        (client_id,),
    )


def test_definite_follow_up_failure_allows_one_deliberate_retry(app_env, monkeypatch):
    client_id = _seed_agent("Retry")
    outcomes: list[Exception | None] = [RuntimeError("provider rejected"), None]
    monkeypatch.setattr(acquisition.mailer, "configured", lambda: True)

    def provider_send(_to: str, _subject: str, _body: str) -> None:
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome

    monkeypatch.setattr(acquisition.mailer, "send_for_studio", provider_send)

    with pytest.raises(HTTPException) as first:
        acquisition.send_follow_up_email(client_id)
    assert first.value.status_code == 502
    failed = db.one(
        """SELECT status, attempts FROM acquisition_email_intents
           WHERE studio_id='default' AND client_id=? AND email_kind='follow_up'""",
        (client_id,),
    )
    assert dict(failed) == {"status": "failed", "attempts": 1}

    assert acquisition.send_follow_up_email(client_id)["status"] == "sent"
    sent = db.one(
        """SELECT status, attempts FROM acquisition_email_intents
           WHERE studio_id='default' AND client_id=? AND email_kind='follow_up'""",
        (client_id,),
    )
    assert dict(sent) == {"status": "sent", "attempts": 2}
    assert outcomes == []


def test_provider_accept_then_local_confirmation_failure_stays_closed(app_env, monkeypatch):
    client_id = _seed_agent("Persistence")
    provider_calls = 0
    monkeypatch.setattr(acquisition.mailer, "configured", lambda: True)

    def provider_send(_to: str, _subject: str, _body: str) -> None:
        nonlocal provider_calls
        provider_calls += 1

    monkeypatch.setattr(acquisition.mailer, "send_for_studio", provider_send)
    monkeypatch.setattr(
        acquisition,
        "_mark_email_intent_sent",
        lambda _intent_id, _claim_token: (_ for _ in ()).throw(
            RuntimeError("database unavailable")
        ),
    )

    with pytest.raises(HTTPException) as accepted:
        acquisition.send_intro_email(client_id)
    assert accepted.value.status_code == 502
    assert "local confirmation failed" in accepted.value.detail
    intent = db.one(
        """SELECT status, attempts, error FROM acquisition_email_intents
           WHERE studio_id='default' AND client_id=? AND email_kind='intro'""",
        (client_id,),
    )
    assert intent["status"] == "unknown"
    assert intent["attempts"] == 1
    assert intent["error"] == "database unavailable"

    assert acquisition.send_intro_email(client_id)["status"] == "review"
    assert provider_calls == 1


def test_reconciliation_is_tenant_scoped_and_rejects_fresh_claim(app_env):
    client_id = _seed_agent("Scoped")
    intent_id = db.run(
        """INSERT INTO acquisition_email_intents
           (studio_id, client_id, email_kind, event_key, to_email, subject, claim_token)
           VALUES ('default', ?, 'intro', ?, 'scoped@example.test', 'Scoped', 'scoped-claim')""",
        (client_id, f"acquisition:intro:{client_id}:after:first"),
    )
    with pytest.raises(HTTPException) as fresh:
        acquisition.reconcile_email_intent(intent_id, client_id=client_id, delivered=False)
    assert fresh.value.status_code == 409

    db.run(
        """UPDATE acquisition_email_intents
           SET claimed_at=datetime('now','-11 minutes') WHERE id=?""",
        (intent_id,),
    )
    acquisition.reconcile_email_intent(intent_id, client_id=client_id, delivered=False)
    assert (
        db.one("SELECT status FROM acquisition_email_intents WHERE id=?", (intent_id,))["status"]
        == "failed"
    )

    db.run("INSERT INTO studio (id, name, slug) VALUES ('other', 'Other', 'other')")
    other_client = db.run(
        """INSERT INTO clients (studio_id, name, email, client_type)
           VALUES ('other', 'Other Agent', 'other@example.test', 'agent')"""
    )
    other_intent = db.run(
        """INSERT INTO acquisition_email_intents
           (studio_id, client_id, email_kind, event_key, to_email, subject, status,
            claim_token)
           VALUES ('other', ?, 'intro', ?, 'other@example.test', 'Other', 'unknown',
                   'other-claim')""",
        (other_client, f"acquisition:intro:{other_client}:after:first"),
    )
    with pytest.raises(HTTPException) as cross_tenant:
        acquisition.reconcile_email_intent(
            other_intent,
            client_id=other_client,
            delivered=True,
        )
    assert cross_tenant.value.status_code == 404
    assert acquisition.unresolved_email_intents() == []


def test_stale_worker_cannot_finalize_a_newer_retry_attempt(app_env):
    client_id = _seed_agent("Fenced")
    draft = acquisition.build_intro_email(client_id)
    first = acquisition._claim_email_intent(
        draft,
        email_kind="intro",
        cooldown_days=acquisition.COOLDOWN_DAYS,
    )
    intent_id = first["intent_id"]
    first_token = first["claim_token"]
    db.run(
        """UPDATE acquisition_email_intents
           SET claimed_at=datetime('now','-11 minutes') WHERE id=?""",
        (intent_id,),
    )
    acquisition.reconcile_email_intent(intent_id, client_id=client_id, delivered=False)

    retry = acquisition._claim_email_intent(
        draft,
        email_kind="intro",
        cooldown_days=acquisition.COOLDOWN_DAYS,
    )
    assert retry["intent_id"] == intent_id
    assert retry["claim_token"] != first_token

    with pytest.raises(RuntimeError, match="no longer active"):
        acquisition._mark_email_intent_sent(intent_id, first_token)
    acquisition._mark_email_intent_failed(
        intent_id,
        first_token,
        RuntimeError("late stale failure"),
        outcome_unknown=True,
    )
    active = db.one(
        """SELECT status, attempts, claim_token, error
           FROM acquisition_email_intents WHERE id=?""",
        (intent_id,),
    )
    assert dict(active) == {
        "status": "claimed",
        "attempts": 2,
        "claim_token": retry["claim_token"],
        "error": None,
    }

    acquisition._mark_email_intent_sent(intent_id, retry["claim_token"])
    assert (
        db.one("SELECT status FROM acquisition_email_intents WHERE id=?", (intent_id,))["status"]
        == "sent"
    )


def test_bulk_unknown_outcome_is_reported_for_provider_review(app_env, monkeypatch):
    client_id = _seed_agent("Bulkunknown")
    monkeypatch.setattr(acquisition.mailer, "configured", lambda: True)
    monkeypatch.setattr(
        acquisition.mailer,
        "send_for_studio",
        lambda *_: (_ for _ in ()).throw(
            acquisition.mailer.DeliveryOutcomeUnknown("provider timeout")
        ),
    )
    monkeypatch.setattr(
        acquisition,
        "intro_ask_queue",
        lambda limit=50: [
            {
                "id": client_id,
                "email": "bulkunknown@example.test",
                "n_active_codes": 0,
                "referral_uses": 0,
                "can_email_intro": True,
                "cooldown_active": False,
            }
        ],
    )

    result = acquisition.bulk_send_intro_emails(queue_filter="ready")

    assert result["review"] == 1
    assert result["failed"] == 0
    assert result["sent"] == 0


@pytest.mark.asyncio
async def test_operator_can_see_and_reconcile_unknown_acquisition_delivery(app_env, monkeypatch):
    client_id = _seed_agent("Operator")
    monkeypatch.setattr(acquisition.mailer, "configured", lambda: True)
    monkeypatch.setattr(
        acquisition.mailer,
        "send_for_studio",
        lambda *_: (_ for _ in ()).throw(
            acquisition.mailer.DeliveryOutcomeUnknown("provider timeout")
        ),
    )
    with pytest.raises(HTTPException):
        acquisition.send_intro_email(client_id)
    intent_id = db.one(
        """SELECT id FROM acquisition_email_intents
           WHERE studio_id='default' AND client_id=?""",
        (client_id,),
    )["id"]

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"password": "test-admin-pass"},
            follow_redirects=False,
        )
        cookie = login.headers["set-cookie"]
        page = await client.get("/admin/reports/acquisition", headers={"cookie": cookie})
        assert page.status_code == 200
        assert "Email delivery review" in page.text
        assert "Operator Agent" in page.text
        assert "provider timeout" in page.text

        reconcile = await client.post(
            f"/admin/reports/acquisition/{client_id}/intents/{intent_id}/reconcile",
            data={"outcome": "delivered"},
            headers={"cookie": cookie},
            follow_redirects=False,
        )
    assert reconcile.status_code == 303
    assert "acquisition=reconciled-sent" in reconcile.headers["location"]
    assert (
        db.one("SELECT status FROM acquisition_email_intents WHERE id=?", (intent_id,))["status"]
        == "sent"
    )
