"""Durable and replay-safe signup verification delivery."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from shutil import copy2
from unittest.mock import Mock

import pytest
from eos import config, db, mailer, signup_verify
from fastapi import HTTPException


def _seed_studio(studio_id: str) -> str:
    email = f"owner@{studio_id}.test"
    db.run(
        """INSERT INTO studio
           (id, name, slug, contact_email, active, signup_verified,
            provisioning_status)
           VALUES (?,?,?,?,1,0,'provisioning')""",
        (studio_id, f"{studio_id.title()} Studio", studio_id, email),
    )
    return email


def _intent(studio_id: str) -> dict:
    row = db.one(
        """SELECT status, attempts, generation, claim_token, claimed_at,
                  sent_at, error, to_email
           FROM signup_verification_intents WHERE studio_id=?""",
        (studio_id,),
    )
    assert row
    return dict(row)


def test_definite_failure_recovers_immediately_with_same_token(app_env_http, monkeypatch):
    email = _seed_studio("verify-failed")
    monkeypatch.setattr(signup_verify.mailer, "configured", lambda: True)
    failed_send = Mock(side_effect=RuntimeError("provider rejected message"))
    monkeypatch.setattr(signup_verify.mailer, "send_platform", failed_send)

    with pytest.raises(RuntimeError, match="provider rejected"):
        signup_verify.issue_token("verify-failed", email=email)

    first_token = db.one("SELECT signup_verify_token FROM studio WHERE id='verify-failed'")[
        "signup_verify_token"
    ]
    assert first_token
    assert _intent("verify-failed")["status"] == "failed"

    delivered = Mock()
    monkeypatch.setattr(signup_verify.mailer, "send_platform", delivered)
    recovered_token = signup_verify.resend("verify-failed")

    assert recovered_token == first_token
    assert delivered.call_count == 1
    intent = _intent("verify-failed")
    assert intent["status"] == "sent"
    assert intent["attempts"] == 2
    assert intent["sent_at"]


def test_unknown_provider_outcome_blocks_blind_replay(app_env_http, monkeypatch):
    email = _seed_studio("verify-unknown")
    monkeypatch.setattr(signup_verify.mailer, "configured", lambda: True)
    provider = Mock(side_effect=mailer.DeliveryOutcomeUnknown("provider timeout after dispatch"))
    monkeypatch.setattr(signup_verify.mailer, "send_platform", provider)

    with pytest.raises(mailer.DeliveryOutcomeUnknown):
        signup_verify.issue_token("verify-unknown", email=email)
    assert _intent("verify-unknown")["status"] == "unknown"

    replay = Mock()
    monkeypatch.setattr(signup_verify.mailer, "send_platform", replay)
    with pytest.raises(HTTPException) as exc_info:
        signup_verify.resend("verify-unknown")

    assert exc_info.value.status_code == 409
    assert "provider" in str(exc_info.value.detail).lower()
    replay.assert_not_called()
    assert _intent("verify-unknown")["attempts"] == 1


def test_provider_accept_with_local_confirmation_failure_is_unknown(
    app_env_http,
    monkeypatch,
):
    email = _seed_studio("verify-local-failure")
    monkeypatch.setattr(signup_verify.mailer, "configured", lambda: True)
    provider = Mock()
    monkeypatch.setattr(signup_verify.mailer, "send_platform", provider)
    monkeypatch.setattr(
        signup_verify,
        "_mark_sent",
        Mock(side_effect=RuntimeError("local commit failed")),
    )

    with pytest.raises(mailer.DeliveryOutcomeUnknown, match="local confirmation"):
        signup_verify.issue_token("verify-local-failure", email=email)

    assert provider.call_count == 1
    intent = _intent("verify-local-failure")
    assert intent["status"] == "unknown"
    assert "local commit failed" in intent["error"]


def test_concurrent_resend_claims_exactly_one_provider_attempt(app_env_http, monkeypatch):
    email = _seed_studio("verify-race")
    monkeypatch.setattr(signup_verify.mailer, "configured", lambda: True)
    monkeypatch.setattr(
        signup_verify.mailer,
        "send_platform",
        Mock(side_effect=RuntimeError("definite initial rejection")),
    )
    with pytest.raises(RuntimeError):
        signup_verify.issue_token("verify-race", email=email)

    provider_entered = threading.Event()
    release_provider = threading.Event()
    calls_lock = threading.Lock()
    provider_calls = 0

    def provider_send(*_args, **_kwargs) -> None:
        nonlocal provider_calls
        with calls_lock:
            provider_calls += 1
        provider_entered.set()
        assert release_provider.wait(timeout=5)

    monkeypatch.setattr(signup_verify.mailer, "send_platform", provider_send)

    def resend() -> tuple[str, int | str]:
        try:
            return "sent", signup_verify.resend("verify-race")
        except HTTPException as exc:
            return "blocked", exc.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(resend)
        assert provider_entered.wait(timeout=5)
        second = executor.submit(resend)
        try:
            completed, _pending = wait((first, second), timeout=5, return_when=FIRST_COMPLETED)
            assert completed == {second}
        finally:
            release_provider.set()
        outcomes = [first.result(timeout=5), second.result(timeout=5)]

    assert sorted(kind for kind, _value in outcomes) == ["blocked", "sent"]
    assert next(value for kind, value in outcomes if kind == "blocked") == 409
    assert provider_calls == 1
    assert _intent("verify-race")["status"] == "sent"
    assert _intent("verify-race")["attempts"] == 2


def test_sent_delivery_is_idempotent_and_resend_has_atomic_cooldown(
    app_env_http,
    monkeypatch,
):
    email = _seed_studio("verify-cooldown")
    monkeypatch.setattr(signup_verify.mailer, "configured", lambda: True)
    provider = Mock()
    monkeypatch.setattr(signup_verify.mailer, "send_platform", provider)

    token = signup_verify.issue_token("verify-cooldown", email=email)
    assert signup_verify.issue_token("verify-cooldown", email=email) == token
    with pytest.raises(HTTPException) as exc_info:
        signup_verify.resend("verify-cooldown")
    assert exc_info.value.status_code == 429
    assert provider.call_count == 1

    db.run(
        """UPDATE signup_verification_intents
           SET sent_at=datetime('now','-6 minutes')
           WHERE studio_id='verify-cooldown'"""
    )
    assert signup_verify.resend("verify-cooldown") == token
    assert provider.call_count == 2
    assert _intent("verify-cooldown")["attempts"] == 2


def test_stale_claim_becomes_unknown_instead_of_replaying(app_env_http, monkeypatch):
    email = _seed_studio("verify-stale")
    monkeypatch.setattr(signup_verify.mailer, "configured", lambda: True)
    claim = signup_verify._claim_delivery(
        "verify-stale",
        email=email,
        resend=False,
    )
    assert claim["status"] == "claimed"
    db.run(
        """UPDATE signup_verification_intents
           SET claimed_at=datetime('now','-11 minutes')
           WHERE studio_id='verify-stale'"""
    )
    provider = Mock()
    monkeypatch.setattr(signup_verify.mailer, "send_platform", provider)

    with pytest.raises(HTTPException) as exc_info:
        signup_verify.resend("verify-stale")

    assert exc_info.value.status_code == 409
    assert _intent("verify-stale")["status"] == "unknown"
    provider.assert_not_called()


def test_stale_worker_cannot_finalize_a_newer_retry(app_env_http, monkeypatch):
    email = _seed_studio("verify-fenced")
    monkeypatch.setattr(signup_verify.mailer, "configured", lambda: True)
    first_claim = signup_verify._claim_delivery(
        "verify-fenced",
        email=email,
        resend=False,
    )
    first_claim_token = first_claim["claim_token"]
    db.run(
        """UPDATE signup_verification_intents
           SET claimed_at=datetime('now','-11 minutes')
           WHERE studio_id='verify-fenced'"""
    )
    assert signup_verify.reconcile_delivery("verify-fenced", delivered=False) == "failed"

    retry_claim = signup_verify._claim_delivery(
        "verify-fenced",
        email=email,
        resend=True,
    )
    retry_claim_token = retry_claim["claim_token"]
    assert retry_claim_token != first_claim_token
    assert _intent("verify-fenced")["generation"] == 2

    with pytest.raises(RuntimeError, match="claim is no longer active"):
        signup_verify._mark_sent("verify-fenced", first_claim_token)
    with pytest.raises(RuntimeError, match="claim is no longer active"):
        signup_verify._mark_failed(
            "verify-fenced",
            first_claim_token,
            RuntimeError("late old failure"),
            outcome_unknown=True,
        )

    active = _intent("verify-fenced")
    assert active["status"] == "claimed"
    assert active["claim_token"] == retry_claim_token
    signup_verify._mark_sent("verify-fenced", retry_claim_token)
    finalized = _intent("verify-fenced")
    assert finalized["status"] == "sent"
    assert finalized["claim_token"] is None


def test_unconfigured_mailer_keeps_local_auto_verify_behavior(app_env_http, monkeypatch):
    email = _seed_studio("verify-local")
    monkeypatch.setattr(signup_verify.mailer, "configured", lambda: False)
    monkeypatch.setattr(signup_verify.config, "SIGNUP_AUTO_VERIFY_LOCAL", True)
    provider = Mock()
    monkeypatch.setattr(signup_verify.mailer, "send_platform", provider)

    token = signup_verify.issue_token("verify-local", email=email)

    assert token
    studio = db.one(
        """SELECT signup_verified, signup_verify_token, signup_verify_issued_at
           FROM studio WHERE id='verify-local'"""
    )
    assert dict(studio) == {
        "signup_verified": 1,
        "signup_verify_token": None,
        "signup_verify_issued_at": None,
    }
    assert (
        db.one(
            """SELECT 1 AS x FROM signup_verification_intents
           WHERE studio_id='verify-local'"""
        )
        is None
    )
    provider.assert_not_called()


def test_unconfigured_hosted_signup_stays_unverified(app_env_http, monkeypatch):
    email = _seed_studio("verify-hosted")
    monkeypatch.setattr(signup_verify.mailer, "configured", lambda: False)
    monkeypatch.setattr(signup_verify.config, "SIGNUP_AUTO_VERIFY_LOCAL", False)

    with pytest.raises(RuntimeError, match="hosted signup remains unverified"):
        signup_verify.issue_token("verify-hosted", email=email)

    studio = db.one(
        """SELECT signup_verified, signup_verify_token, signup_verify_issued_at
           FROM studio WHERE id='verify-hosted'"""
    )
    assert dict(studio) == {
        "signup_verified": 0,
        "signup_verify_token": None,
        "signup_verify_issued_at": None,
    }
    assert (
        db.one(
            """SELECT 1 AS x FROM signup_verification_intents
           WHERE studio_id='verify-hosted'"""
        )
        is None
    )


def test_verification_delivery_and_token_state_are_tenant_scoped(app_env_http, monkeypatch):
    alpha_email = _seed_studio("verify-alpha")
    beta_email = _seed_studio("verify-beta")
    monkeypatch.setattr(signup_verify.mailer, "configured", lambda: True)
    provider = Mock()
    monkeypatch.setattr(signup_verify.mailer, "send_platform", provider)

    alpha_token = signup_verify.issue_token("verify-alpha", email=alpha_email)

    assert alpha_token
    assert provider.call_args.args[0] == alpha_email
    assert (
        db.one("""SELECT signup_verify_token FROM studio WHERE id='verify-beta'""")[
            "signup_verify_token"
        ]
        is None
    )
    assert (
        db.one(
            """SELECT 1 AS x FROM signup_verification_intents
           WHERE studio_id='verify-beta'"""
        )
        is None
    )

    with pytest.raises(HTTPException) as exc_info:
        signup_verify.issue_token("verify-alpha", email=beta_email, resend=True)
    assert exc_info.value.status_code == 409
    assert provider.call_count == 1


def test_successful_token_verification_closes_delivery_intent(app_env_http, monkeypatch):
    email = _seed_studio("verify-token")
    monkeypatch.setattr(signup_verify.mailer, "configured", lambda: True)
    monkeypatch.setattr(signup_verify.mailer, "send_platform", Mock())
    token = signup_verify.issue_token("verify-token", email=email)

    assert signup_verify.verify_token(token) == "verify-token"
    studio = db.one(
        """SELECT signup_verified, signup_verify_token, signup_verify_issued_at
           FROM studio WHERE id='verify-token'"""
    )
    assert dict(studio) == {
        "signup_verified": 1,
        "signup_verify_token": None,
        "signup_verify_issued_at": None,
    }
    assert _intent("verify-token")["status"] == "verified"
    with pytest.raises(HTTPException) as replay:
        signup_verify.verify_token(token)
    assert replay.value.status_code == 404


def test_migration_backfills_legacy_delivery_state_fail_closed(tmp_path, monkeypatch):
    database = tmp_path / "data" / "eos.db"
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    source_migrations = db.MIGRATIONS_DIR
    monkeypatch.setattr(config, "DB_PATH", database)
    monkeypatch.setattr(
        config,
        "ensure_dirs",
        lambda: database.parent.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations_dir)
    for source in source_migrations.glob("*.sql"):
        if int(source.name.split("_", 1)[0]) <= 20:
            copy2(source, migrations_dir / source.name)
    db.migrate()

    with sqlite3.connect(database) as con:
        con.executescript(
            """
            UPDATE studio
            SET contact_email='unknown@example.test', signup_verified=0,
                signup_verify_token='legacy-unknown-token',
                signup_verify_issued_at='2026-07-01 10:00:00',
                provisioning_status='degraded',
                provisioning_error='verification: legacy provider timeout'
            WHERE id='default';
            INSERT INTO studio
                (id, name, slug, contact_email, active, signup_verified,
                 signup_verify_token, signup_verify_issued_at,
                 provisioning_status)
            VALUES
                ('legacy-sent', 'Legacy Sent', 'legacy-sent', 'sent@example.test',
                 1, 0, 'legacy-sent-token', '2026-07-01 11:00:00', 'ready');
            """
        )

    copy2(
        source_migrations / "0021_signup_verification_durability.sql",
        migrations_dir / "0021_signup_verification_durability.sql",
    )
    db.migrate()
    db.migrate()

    with sqlite3.connect(database) as con:
        unknown = con.execute(
            """SELECT token_fingerprint, status, sent_at, error
               FROM signup_verification_intents WHERE studio_id='default'"""
        ).fetchone()
        sent = con.execute(
            """SELECT token_fingerprint, status, sent_at, error
               FROM signup_verification_intents WHERE studio_id='legacy-sent'"""
        ).fetchone()
        foreign_key_errors = con.execute("PRAGMA foreign_key_check").fetchall()

    assert unknown == (
        "nknown-token",
        "unknown",
        None,
        "Legacy verification delivery outcome requires provider review",
    )
    assert sent == ("y-sent-token", "sent", "2026-07-01 11:00:00", None)
    assert foreign_key_errors == []
