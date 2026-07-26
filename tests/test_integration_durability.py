"""Replay, concurrency, and failure boundaries for external integrations."""

from __future__ import annotations

import importlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from shutil import copy2

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.oauth_store as oauth_store
import eos.secret_store as secret_store
import eos.studio as studio
import eos.tenant as tenant
import eos.users as users
import httpx
import pytest
from httpx import ASGITransport, AsyncClient


class Response:
    def __init__(
        self,
        status_code: int = 200,
        *,
        data: dict | None = None,
        content: bytes = b"",
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._data = data or {}
        self.content = content
        self.headers = headers or {}

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://provider.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"provider returned {self.status_code}",
                request=request,
                response=response,
            )


@pytest.fixture()
def integration_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_GOOGLE_CLIENT_ID", "gclient")
    monkeypatch.setenv("EOS_GOOGLE_CLIENT_SECRET", "gsecret")
    monkeypatch.setenv("EOS_GOOGLE_REDIRECT_URI", "http://test/oauth/google/callback")
    monkeypatch.setenv("EOS_DROPBOX_APP_KEY", "dkey")
    monkeypatch.setenv("EOS_DROPBOX_APP_SECRET", "dsecret")
    monkeypatch.setenv("EOS_DROPBOX_REDIRECT_URI", "http://test/oauth/dropbox/callback")
    for module in (config, db, jobs, tenant, secret_store, oauth_store, studio):
        importlib.reload(module)
    import eos.integrations.dropbox as dropbox
    import eos.integrations.google_calendar as google_calendar

    importlib.reload(google_calendar)
    importlib.reload(dropbox)
    config.ensure_dirs()
    db.migrate()
    tenant.set_studio("default")
    studio.get_profile()
    oauth_store.save_tokens("google", access_token="google-token")
    oauth_store.save_tokens("dropbox", access_token="dropbox-token")
    db.run(
        """UPDATE studio_profiles
           SET google_calendar_enabled=1, dropbox_enabled=1,
               dropbox_watch_path='/Eos Ingest'
           WHERE studio_id='default'"""
    )
    monkeypatch.setattr(jobs, "_submit", lambda _job_id: None)
    return google_calendar, dropbox


def _appointment(*, status: str = "confirmed", event_id: str | None = None) -> int:
    return db.run(
        """INSERT INTO appointments
           (studio_id, title, kind, status, starts_at, token, google_event_id)
           VALUES ('default','Shoot','shoot',?,'2026-08-01 10:00:00',?,?)""",
        (status, f"tok-{status}-{event_id or 'new'}", event_id),
    )


def _listing() -> int:
    return db.run(
        """INSERT INTO listings (studio_id, title, status)
           VALUES ('default','123 Main','booked')"""
    )


def _dropbox_entry(listing_id: int, *, revision: str = "rev-1") -> dict:
    path = f"/Eos Ingest/{listing_id}/front.jpg"
    return {
        ".tag": "file",
        "id": "id:file-1",
        "rev": revision,
        "content_hash": f"hash-{revision}",
        "path_display": path,
        "path_lower": path.lower(),
    }


def _scan_response(entry: dict, *, cursor: str = "cursor-1") -> Response:
    return Response(data={"entries": [entry], "cursor": cursor, "has_more": False})


def _download_response(entry: dict) -> Response:
    metadata = {
        "id": entry["id"],
        "rev": entry["rev"],
        "content_hash": entry["content_hash"],
    }
    return Response(
        content=b"photo-bytes",
        headers={"Dropbox-API-Result": json.dumps(metadata)},
    )


def test_migration_0020_preserves_legacy_integration_state(tmp_path, monkeypatch):
    database = tmp_path / "legacy" / "eos.db"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    source_migrations = db.MIGRATIONS_DIR
    monkeypatch.setattr(config, "DB_PATH", database)
    monkeypatch.setattr(
        config,
        "ensure_dirs",
        lambda: database.parent.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    for source in source_migrations.glob("*.sql"):
        if int(source.name.split("_", 1)[0]) <= 19:
            copy2(source, migrations / source.name)
    db.migrate()
    with sqlite3.connect(database) as con:
        con.executescript(
            """
            INSERT INTO listings (id, studio_id, title, status)
            VALUES (700, 'default', 'Legacy listing', 'booked');
            INSERT INTO appointments
                (id, studio_id, title, kind, status, starts_at, token,
                 google_event_id)
            VALUES
                (701, 'default', 'Legacy shoot', 'shoot', 'confirmed',
                 '2026-07-01 10:00:00', 'legacy-google-token', 'legacy-event');
            INSERT INTO dropbox_ingest_log
                (id, studio_id, dropbox_path, listing_id, status, error)
            VALUES
                (702, 'default', '/Eos Ingest/700/legacy.jpg', 700, 'failed',
                 'legacy failure');
            """
        )
    copy2(
        source_migrations / "0020_integrations_durability.sql",
        migrations / "0020_integrations_durability.sql",
    )
    db.migrate()
    db.migrate()

    with sqlite3.connect(database) as con:
        intent = con.execute(
            """SELECT event_id, provider_bound, status
               FROM google_calendar_sync_intents WHERE appointment_id=701"""
        ).fetchone()
        ingest = con.execute(
            """SELECT dropbox_path_lower, provider_file_id, provider_revision,
                      provider_key, status, error
               FROM dropbox_ingest_log WHERE id=702"""
        ).fetchone()
        foreign_key_errors = con.execute("PRAGMA foreign_key_check").fetchall()
        version = con.execute(
            "SELECT version FROM schema_migrations ORDER BY CAST(version AS INTEGER) DESC LIMIT 1"
        ).fetchone()

    assert intent == ("legacy-event", 1, "synced")
    assert ingest == (
        "/eos ingest/700/legacy.jpg",
        "",
        "",
        "legacy:702:/eos ingest/700/legacy.jpg",
        "failed",
        "legacy failure",
    )
    assert foreign_key_errors == []
    assert version == ("0020",)


def test_google_create_is_deterministic_and_concurrent_first_push_is_safe(
    integration_env, monkeypatch
):
    google, _dropbox = integration_env
    appt_id = _appointment()
    google.enqueue_push(appt_id)
    google.enqueue_push(appt_id)
    assert db.one("SELECT COUNT(*) AS n FROM jobs")["n"] == 1
    event_id = db.one("SELECT google_event_id FROM appointments WHERE id=?", (appt_id,))[
        "google_event_id"
    ]
    assert event_id.startswith("eos")

    entered = threading.Event()
    release = threading.Event()
    calls: list[dict] = []

    def post(_url, **kwargs):
        calls.append(kwargs["json"])
        entered.set()
        release.wait(timeout=5)
        return Response(data={"id": event_id})

    monkeypatch.setattr(google.httpx, "post", post)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(google.push_appointment, appt_id)
        assert entered.wait(timeout=5)
        second = pool.submit(google.push_appointment, appt_id)
        with pytest.raises(google.GoogleSyncOutcomeUnknown):
            second.result(timeout=5)
        release.set()
        first.result(timeout=5)

    assert calls == [
        {
            **json.loads(
                db.one(
                    """SELECT payload FROM google_calendar_sync_intents
                       WHERE studio_id='default' AND appointment_id=?""",
                    (appt_id,),
                )["payload"]
            ),
            "id": event_id,
        }
    ]
    intent = db.one(
        """SELECT status, provider_bound FROM google_calendar_sync_intents
           WHERE studio_id='default' AND appointment_id=?""",
        (appt_id,),
    )
    assert (intent["status"], intent["provider_bound"]) == ("synced", 1)
    google.push_appointment(appt_id)
    assert len(calls) == 1


def test_google_provider_500_propagates_and_failed_delete_keeps_binding(
    integration_env, monkeypatch
):
    google, _dropbox = integration_env
    appt_id = _appointment(status="canceled", event_id="legacy-event")
    monkeypatch.setattr(google.httpx, "delete", lambda *_args, **_kwargs: Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        google.push_appointment(appt_id)

    appointment = db.one("SELECT google_event_id FROM appointments WHERE id=?", (appt_id,))
    intent = db.one(
        """SELECT status, last_error FROM google_calendar_sync_intents
           WHERE studio_id='default' AND appointment_id=?""",
        (appt_id,),
    )
    assert appointment["google_event_id"] == "legacy-event"
    assert intent["status"] == "failed"
    assert "500" in intent["last_error"]


def test_google_provider_success_db_failure_is_unknown_and_not_reposted(
    integration_env, monkeypatch
):
    google, _dropbox = integration_env
    appt_id = _appointment()
    event_id = google._deterministic_event_id("default", appt_id)
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return Response(data={"id": event_id})

    monkeypatch.setattr(google.httpx, "post", post)
    monkeypatch.setattr(
        google,
        "_complete_push",
        lambda _intent: (_ for _ in ()).throw(sqlite3.OperationalError("commit failed")),
    )
    with pytest.raises(google.GoogleSyncOutcomeUnknown):
        google.push_appointment(appt_id)
    assert (
        db.one(
            """SELECT status FROM google_calendar_sync_intents
           WHERE studio_id='default' AND appointment_id=?""",
            (appt_id,),
        )["status"]
        == "unknown"
    )
    with pytest.raises(google.GoogleSyncOutcomeUnknown):
        google.push_appointment(appt_id)
    assert calls == 1


def test_google_successful_delete_does_not_restore_binding(integration_env, monkeypatch):
    google, _dropbox = integration_env
    appt_id = _appointment(status="canceled", event_id="legacy-event")
    calls = 0

    def delete(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return Response(204)

    monkeypatch.setattr(google.httpx, "delete", delete)
    google.push_appointment(appt_id)
    google.push_appointment(appt_id)
    assert (
        db.one("SELECT google_event_id FROM appointments WHERE id=?", (appt_id,))["google_event_id"]
        is None
    )
    assert calls == 1


def test_google_update_uses_same_binding_and_is_idempotent(integration_env, monkeypatch):
    google, _dropbox = integration_env
    appt_id = _appointment(event_id="existing-event")
    patches: list[dict] = []

    def patch(_url, **kwargs):
        patches.append(kwargs["json"])
        return Response(data={"id": "existing-event"})

    monkeypatch.setattr(google.httpx, "patch", patch)
    google.push_appointment(appt_id)
    db.run(
        "UPDATE appointments SET title='Updated shoot' WHERE id=? AND studio_id='default'",
        (appt_id,),
    )
    google.enqueue_push(appt_id)
    google.push_appointment(appt_id)
    google.push_appointment(appt_id)

    assert [body["summary"] for body in patches] == ["Shoot", "Updated shoot"]
    assert (
        db.one("SELECT google_event_id FROM appointments WHERE id=?", (appt_id,))["google_event_id"]
        == "existing-event"
    )
    intent = db.one(
        """SELECT status, revision FROM google_calendar_sync_intents
           WHERE studio_id='default' AND appointment_id=?""",
        (appt_id,),
    )
    assert (intent["status"], intent["revision"]) == ("synced", 2)


def test_google_pull_failure_propagates_for_job_retry(integration_env, monkeypatch):
    google, _dropbox = integration_env
    monkeypatch.setattr(google.httpx, "get", lambda *_args, **_kwargs: Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        google.pull_changes()

    profile = db.one("SELECT google_last_sync_error FROM studio_profiles WHERE studio_id='default'")
    assert "500" in profile["google_last_sync_error"]


def test_dropbox_two_concurrent_scans_reserve_one_log_and_one_job(integration_env, monkeypatch):
    _google, dropbox = integration_env
    listing_id = _listing()
    entry = _dropbox_entry(listing_id)
    barrier = threading.Barrier(2)

    def post(*_args, **_kwargs):
        barrier.wait(timeout=5)
        return _scan_response(entry)

    monkeypatch.setattr(dropbox.httpx, "post", post)
    with ThreadPoolExecutor(max_workers=2) as pool:
        queued = list(pool.map(lambda _n: dropbox.scan_folder(), range(2)))

    assert sorted(queued) == [0, 1]
    assert db.one("SELECT COUNT(*) AS n FROM dropbox_ingest_log")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='dropbox_ingest'")["n"] == 1
    row = db.one("SELECT * FROM dropbox_ingest_log")
    assert row["provider_file_id"] == entry["id"]
    assert row["provider_revision"] == entry["rev"]
    assert row["job_id"] is not None


def test_dropbox_crash_replay_uses_stable_file_and_creates_one_asset(integration_env, monkeypatch):
    _google, dropbox = integration_env
    listing_id = _listing()
    entry = _dropbox_entry(listing_id)
    monkeypatch.setattr(
        dropbox.httpx,
        "post",
        lambda url, **_kwargs: (
            _download_response(entry) if "content.dropboxapi.com" in url else _scan_response(entry)
        ),
    )
    assert dropbox.scan_folder() == 1
    log_row = db.one("SELECT * FROM dropbox_ingest_log")
    original_finalize = dropbox._finalize_ingest
    monkeypatch.setattr(
        dropbox,
        "_finalize_ingest",
        lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("commit failed")),
    )
    with pytest.raises(sqlite3.OperationalError):
        dropbox.ingest_file(
            log_id=log_row["id"],
            dropbox_path=log_row["dropbox_path"],
            listing_id=listing_id,
        )
    failed = db.one("SELECT status, stored FROM dropbox_ingest_log WHERE id=?", (log_row["id"],))
    assert failed["status"] == "failed"
    monkeypatch.setattr(dropbox, "_finalize_ingest", original_finalize)
    dropbox.ingest_file(
        log_id=log_row["id"],
        dropbox_path=log_row["dropbox_path"],
        listing_id=listing_id,
    )
    dropbox.ingest_file(
        log_id=log_row["id"],
        dropbox_path=log_row["dropbox_path"],
        listing_id=listing_id,
    )

    completed = db.one("SELECT * FROM dropbox_ingest_log WHERE id=?", (log_row["id"],))
    assert completed["status"] == "done"
    assert completed["stored"].startswith("dbx-")
    assert db.one("SELECT COUNT(*) AS n FROM assets")["n"] == 1
    assert (
        db.one(
            """SELECT COUNT(*) AS n FROM jobs
           WHERE kind='image_derivatives'"""
        )["n"]
        == 1
    )
    assert (
        db.one(
            """SELECT COUNT(*) AS n FROM audit_log
           WHERE action='dropbox.ingest'"""
        )["n"]
        == 1
    )


def test_dropbox_stale_job_error_cannot_fail_newer_completed_claim(integration_env, monkeypatch):
    _google, dropbox = integration_env
    listing_id = _listing()
    entry = _dropbox_entry(listing_id)
    monkeypatch.setattr(dropbox.httpx, "post", lambda *_args, **_kwargs: _scan_response(entry))
    assert dropbox.scan_folder() == 1
    log_row = db.one("SELECT * FROM dropbox_ingest_log")
    payload = {
        "studio_id": "default",
        "log_id": log_row["id"],
        "dropbox_path": log_row["dropbox_path"],
        "listing_id": listing_id,
    }
    first_download_started = threading.Event()
    release_first_download = threading.Event()
    calls_lock = threading.Lock()
    download_calls = 0

    def post(url, **_kwargs):
        nonlocal download_calls
        assert "content.dropboxapi.com" in url
        with calls_lock:
            download_calls += 1
            call_number = download_calls
        if call_number == 1:
            first_download_started.set()
            release_first_download.wait(timeout=5)
            return Response(500)
        return _download_response(entry)

    monkeypatch.setattr(dropbox.httpx, "post", post)
    with ThreadPoolExecutor(max_workers=1) as pool:
        stale_worker = pool.submit(jobs._h_dropbox_ingest, payload)
        assert first_download_started.wait(timeout=5)
        db.run(
            """UPDATE dropbox_ingest_log SET claimed_at=datetime('now','-16 minutes')
               WHERE id=? AND studio_id='default'""",
            (log_row["id"],),
        )
        jobs._h_dropbox_ingest(payload)
        release_first_download.set()
        with pytest.raises(httpx.HTTPStatusError):
            stale_worker.result(timeout=5)

    completed = db.one(
        "SELECT status, claim_token, error FROM dropbox_ingest_log WHERE id=?",
        (log_row["id"],),
    )
    assert dict(completed) == {"status": "done", "claim_token": None, "error": None}
    assert db.one("SELECT COUNT(*) AS n FROM assets")["n"] == 1


def test_dropbox_revision_mismatch_fails_without_asset(integration_env, monkeypatch):
    _google, dropbox = integration_env
    listing_id = _listing()
    entry = _dropbox_entry(listing_id)
    monkeypatch.setattr(dropbox.httpx, "post", lambda *_args, **_kwargs: _scan_response(entry))
    dropbox.scan_folder()
    log_row = db.one("SELECT * FROM dropbox_ingest_log")
    mismatched = {**entry, "rev": "different"}
    monkeypatch.setattr(
        dropbox.httpx,
        "post",
        lambda *_args, **_kwargs: _download_response(mismatched),
    )

    with pytest.raises(RuntimeError, match="revision changed"):
        dropbox.ingest_file(
            log_id=log_row["id"],
            dropbox_path=log_row["dropbox_path"],
            listing_id=listing_id,
        )
    assert db.one("SELECT status FROM dropbox_ingest_log")["status"] == "failed"
    assert db.one("SELECT COUNT(*) AS n FROM assets")["n"] == 0


def test_google_reconciliation_refuses_fresh_claim_then_records_applied(
    integration_env, monkeypatch
):
    google, _dropbox = integration_env
    appt_id = _appointment()
    prepared = google._prepare_intent(appt_id, retry_failed=False)
    assert prepared
    event_id = prepared[0]["event_id"]
    db.run(
        """UPDATE google_calendar_sync_intents
           SET status='unknown', attempts=2, last_error='local commit failed',
               updated_at=datetime('now')
           WHERE studio_id='default' AND appointment_id=?""",
        (appt_id,),
    )
    row = google.list_sync_intents()[0]
    assert row["reconcile_ready"] == 0
    with pytest.raises(google.GoogleReconciliationConflict, match="active claim"):
        google.reconcile_unknown(appt_id, applied=True)

    db.run(
        """UPDATE google_calendar_sync_intents
           SET updated_at=datetime('now','-16 minutes')
           WHERE studio_id='default' AND appointment_id=?""",
        (appt_id,),
    )

    def provider_call_forbidden(*_args, **_kwargs):
        raise AssertionError("reconciliation must not call Google")

    for method in ("get", "post", "patch", "delete"):
        monkeypatch.setattr(google.httpx, method, provider_call_forbidden)
    assert google.list_sync_intents()[0]["reconcile_ready"] == 1
    google.reconcile_unknown(appt_id, applied=True)

    intent = db.one(
        """SELECT status, provider_bound, last_error
           FROM google_calendar_sync_intents
           WHERE studio_id='default' AND appointment_id=?""",
        (appt_id,),
    )
    appointment = db.one(
        "SELECT google_event_id, google_synced_at FROM appointments WHERE id=?",
        (appt_id,),
    )
    assert (intent["status"], intent["provider_bound"], intent["last_error"]) == (
        "synced",
        1,
        None,
    )
    assert appointment["google_event_id"] == event_id
    assert appointment["google_synced_at"] is not None
    assert db.one(
        """SELECT id FROM audit_log
           WHERE studio_id='default'
             AND action='integration.google.reconcile.applied'"""
    )


def test_google_reconciliation_not_applied_releases_failed_job_for_retry(
    integration_env,
):
    google, _dropbox = integration_env
    appt_id = _appointment()
    google.enqueue_push(appt_id)
    job = db.one(
        """SELECT id FROM jobs
           WHERE studio_id='default' AND kind='google_calendar_push'"""
    )
    assert job
    db.run(
        """UPDATE jobs SET status='failed', attempts=3, error='outcome unknown'
           WHERE id=? AND studio_id='default'""",
        (job["id"],),
    )
    db.run(
        """UPDATE google_calendar_sync_intents
           SET status='unknown', updated_at=datetime('now','-16 minutes')
           WHERE studio_id='default' AND appointment_id=?""",
        (appt_id,),
    )

    google.reconcile_unknown(appt_id, applied=False)

    intent = db.one(
        """SELECT status, provider_bound, last_error
           FROM google_calendar_sync_intents
           WHERE studio_id='default' AND appointment_id=?""",
        (appt_id,),
    )
    assert intent["status"] == "failed"
    assert intent["provider_bound"] == 0
    assert "not applied" in intent["last_error"]
    assert jobs.retry_job(job["id"])
    retried = db.one("SELECT status, attempts, error FROM jobs WHERE id=?", (job["id"],))
    assert (retried["status"], retried["attempts"], retried["error"]) == (
        "queued",
        0,
        None,
    )
    assert db.one(
        """SELECT id FROM audit_log
           WHERE studio_id='default'
             AND action='integration.google.reconcile.not_applied'"""
    )


def test_integration_operator_lists_and_reconciliation_are_tenant_scoped(
    integration_env,
):
    google, dropbox = integration_env
    default_appt = _appointment()
    default_intent = google._prepare_intent(default_appt, retry_failed=False)
    assert default_intent
    db.run(
        """INSERT INTO dropbox_ingest_log
           (studio_id, dropbox_path, dropbox_path_lower, provider_file_id,
            provider_revision, provider_key, status)
           VALUES ('default','/default.jpg','/default.jpg','id:default',
                   'rev-default','key-default','failed')"""
    )
    db.run("INSERT INTO studio (id, name, slug) VALUES ('beta','Beta','beta')")
    beta_appt = db.run(
        """INSERT INTO appointments
           (studio_id, title, kind, starts_at, token)
           VALUES ('beta','Beta Shoot','shoot','2026-08-02 10:00:00','beta-appt')"""
    )
    db.run(
        """INSERT INTO google_calendar_sync_intents
           (studio_id, appointment_id, event_id, desired_action, payload,
            payload_hash, status, updated_at)
           VALUES ('beta',?,'beta-event','upsert','{}','beta-hash','unknown',
                   datetime('now','-16 minutes'))""",
        (beta_appt,),
    )
    db.run(
        """INSERT INTO dropbox_ingest_log
           (studio_id, dropbox_path, dropbox_path_lower, provider_file_id,
            provider_revision, provider_key, status)
           VALUES ('beta','/beta.jpg','/beta.jpg','id:beta',
                   'rev-beta','key-beta','failed')"""
    )

    assert {r["appointment_id"] for r in google.list_sync_intents()} == {default_appt}
    assert {r["provider_file_id"] for r in dropbox.list_ingest_logs()} == {"id:default"}
    tenant.set_studio("beta")
    try:
        assert {r["appointment_id"] for r in google.list_sync_intents()} == {beta_appt}
        assert {r["provider_file_id"] for r in dropbox.list_ingest_logs()} == {"id:beta"}
        with pytest.raises(google.GoogleIntentNotFound):
            google.reconcile_unknown(default_appt, applied=True)
    finally:
        tenant.set_studio("default")


def _seed_operator_ui_state() -> tuple[int, int]:
    studio.get_profile()
    appt_id = _appointment()
    db.run(
        """INSERT INTO google_calendar_sync_intents
           (studio_id, appointment_id, event_id, desired_action, payload,
            payload_hash, status, attempts, last_error, updated_at)
           VALUES ('default',?,'provider-event-ui','upsert','{}','ui-hash',
                   'unknown',3,'provider outcome unknown',
                   datetime('now','-16 minutes'))""",
        (appt_id,),
    )
    log_id = db.run(
        """INSERT INTO dropbox_ingest_log
           (studio_id, dropbox_path, dropbox_path_lower, provider_file_id,
            provider_revision, provider_key, status, attempts, error)
           VALUES ('default','/Eos Ingest/ui.jpg','/eos ingest/ui.jpg',
                   'id:provider-ui','rev-provider-ui','ui-provider-key',
                   'failed',2,'download failed')"""
    )
    return appt_id, log_id


@pytest.mark.asyncio
async def test_studio_ui_shows_provider_identity_and_reconciles_with_csrf(app_env_http):
    users.create_user(
        "owner@default.test",
        "owner-pass-1",
        role="owner",
        studio_id="default",
    )
    appt_id, _log_id = _seed_operator_ui_state()
    transport = ASGITransport(app=app_env_http)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "owner@default.test", "password": "owner-pass-1"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        page = await client.get("/admin/studio")
        assert page.status_code == 200
        for visible in (
            "provider-event-ui",
            "provider outcome unknown",
            "id:provider-ui",
            "rev-provider-ui",
            "download failed",
            "Provider shows applied",
            "Provider shows not applied",
        ):
            assert visible in page.text

        blocked = await client.post(
            f"/admin/studio/google-intents/{appt_id}/reconcile",
            data={"outcome": "applied"},
            headers={"sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert blocked.status_code == 403
        csrf = client.cookies.get("eos_csrf")
        allowed = await client.post(
            f"/admin/studio/google-intents/{appt_id}/reconcile",
            data={"outcome": "applied", "_csrf": csrf},
            headers={"sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert allowed.status_code == 303
    assert (
        db.one(
            """SELECT status FROM google_calendar_sync_intents
           WHERE studio_id='default' AND appointment_id=?""",
            (appt_id,),
        )["status"]
        == "synced"
    )


@pytest.mark.asyncio
async def test_google_reconciliation_route_is_owner_only(app_env_http):
    users.create_user(
        "operator@default.test",
        "operator-pass-1",
        role="operator",
        studio_id="default",
    )
    appt_id, _log_id = _seed_operator_ui_state()
    transport = ASGITransport(app=app_env_http)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"email": "operator@default.test", "password": "operator-pass-1"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = client.cookies.get("eos_csrf")
        blocked = await client.post(
            f"/admin/studio/google-intents/{appt_id}/reconcile",
            data={"outcome": "applied", "_csrf": csrf},
            headers={"sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert blocked.status_code == 403
    assert (
        db.one(
            """SELECT status FROM google_calendar_sync_intents
           WHERE studio_id='default' AND appointment_id=?""",
            (appt_id,),
        )["status"]
        == "unknown"
    )
