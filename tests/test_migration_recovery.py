"""Crash/retry coverage for atomic numbered migrations."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from shutil import copy2

import pytest
from eos import config, db


def _versions(database) -> tuple[str, ...]:
    con = sqlite3.connect(database)
    try:
        return tuple(
            row[0] for row in con.execute("SELECT version FROM schema_migrations ORDER BY version")
        )
    finally:
        con.close()


def test_failed_migration_rolls_back_and_concurrent_retry_recovers(tmp_path, monkeypatch) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    database = tmp_path / "data" / "eos.db"

    monkeypatch.setattr(config, "DB_PATH", database)
    monkeypatch.setattr(
        config,
        "ensure_dirs",
        lambda: database.parent.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations_dir)

    (migrations_dir / "0001_base.sql").write_text(
        "CREATE TABLE base (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    failing = migrations_dir / "0002_retry.sql"
    failing.write_text(
        """CREATE TABLE partial_schema (value TEXT NOT NULL);
INSERT INTO partial_schema (value) VALUES ('must roll back');
INSERT INTO table_that_does_not_exist (value) VALUES ('fail');
""",
        encoding="utf-8",
    )

    with pytest.raises(sqlite3.OperationalError, match="table_that_does_not_exist"):
        db.migrate()

    with sqlite3.connect(database) as con:
        table_exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='partial_schema'"
        ).fetchone()
    assert table_exists is None
    assert _versions(database) == ("0001",)

    failing.write_text(
        """CREATE TABLE partial_schema (value TEXT NOT NULL);
INSERT INTO partial_schema (value) VALUES ('recovered');
""",
        encoding="utf-8",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(db.migrate) for _ in range(2)]
        for future in futures:
            future.result()

    with sqlite3.connect(database) as con:
        assert con.execute("SELECT value FROM partial_schema").fetchone() == ("recovered",)
    assert _versions(database) == ("0001", "0002")

    db.migrate()
    assert _versions(database) == ("0001", "0002")


def test_beta_integrity_migration_recovers_legacy_manual_paid_deposit(
    tmp_path, monkeypatch
) -> None:
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
        if int(source.name.split("_", 1)[0]) <= 17:
            copy2(source, migrations_dir / source.name)
    db.migrate()
    assert _versions(database)[-1] == "0017"
    with sqlite3.connect(database) as con:
        con.executescript(
            """
            INSERT INTO clients (id, studio_id, name, email)
            VALUES (501, 'default', 'Legacy Agent', 'legacy@example.test');
            INSERT INTO listings
                (id, studio_id, client_id, title, status, updated_at)
            VALUES
                (502, 'default', 501, 'Legacy manual deposit', 'lead',
                 '2020-01-01 00:00:00');
            INSERT INTO appointments
                (id, studio_id, listing_id, client_id, title, status, token)
            VALUES
                (503, 'default', 502, 501, 'Legacy shoot', 'proposed',
                 'legacy-appointment-token');
            INSERT INTO proposals
                (id, studio_id, listing_id, slug, title, status)
            VALUES
                (504, 'default', 502, 'legacy-proposal', 'Legacy proposal', 'draft');
            INSERT INTO invoices
                (id, studio_id, listing_id, client_id, slug, title, amount_cents,
                 status, paid_at, invoice_kind)
            VALUES
                (505, 'default', 502, 501, 'legacy-deposit', 'Legacy deposit',
                 5000, 'paid', '2020-01-02 00:00:00', 'deposit');
            INSERT INTO inquiries
                (id, studio_id, name, email, status, listing_id, client_id,
                 appointment_id, invoice_id, order_token, total_cents, deposit_cents)
            VALUES
                (506, 'default', 'Legacy Agent', 'legacy@example.test',
                 'pending_payment', 502, 501, 503, 505, 'legacy-order-token',
                 17500, 5000);
            UPDATE appointments SET inquiry_id=506 WHERE id=503;
            UPDATE invoices SET inquiry_id=506 WHERE id=505;
            """
        )
    copy2(
        source_migrations / "0018_beta_journey_integrity.sql",
        migrations_dir / "0018_beta_journey_integrity.sql",
    )
    db.migrate()
    with sqlite3.connect(database) as con:
        inquiry = con.execute(
            """SELECT status, payment_expires_at, payment_reconcile_error,
                      payment_reconciled_at
               FROM inquiries WHERE id=506"""
        ).fetchone()
        appointment = con.execute("SELECT status FROM appointments WHERE id=503").fetchone()
        listing = con.execute("SELECT status, updated_at FROM listings WHERE id=502").fetchone()
        proposal = con.execute("SELECT status, sent_at FROM proposals WHERE id=504").fetchone()
    assert inquiry is not None
    assert inquiry[:3] == ("confirmed", None, None)
    assert inquiry[3] is not None
    assert appointment == ("confirmed",)
    assert listing is not None
    assert listing[0] == "booked"
    assert listing[1] != "2020-01-01 00:00:00"
    assert proposal is not None
    assert proposal[0] == "sent"
    assert proposal[1] is not None
    assert _versions(database)[-1] == "0018"


def test_delivery_integrity_migration_preserves_legacy_state_and_marks_delivery(
    tmp_path, monkeypatch
) -> None:
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
        if int(source.name.split("_", 1)[0]) <= 18:
            copy2(source, migrations_dir / source.name)
    db.migrate()
    assert _versions(database)[-1] == "0018"

    with sqlite3.connect(database) as con:
        con.executescript(
            """
            INSERT INTO clients (id, studio_id, name, email)
            VALUES (601, 'default', 'Legacy Delivery Agent', 'delivery@example.test');
            INSERT INTO listings
                (id, studio_id, client_id, title, status, revision_round,
                 delivered_at, updated_at)
            VALUES
                (602, 'default', 601, 'Legacy delivered listing', 'delivered', 2,
                 '2025-04-05 12:00:00', '2025-04-05 12:00:00');
            INSERT INTO galleries
                (id, studio_id, listing_id, slug, title, pin, delivery_token, published)
            VALUES
                (600, 'default', 602, 'older-legacy-delivery-gallery',
                 'Older legacy gallery', '5678', 'older-legacy-delivery-token', 0),
                (603, 'default', 602, 'legacy-delivery-gallery', 'Legacy gallery',
                 '1234', 'legacy-delivery-token', 0);
            INSERT INTO delivery_notifications
                (id, studio_id, gallery_id, status, attempts, claimed_at, error,
                 created_at, updated_at)
            VALUES
                (599, 'default', 600, 'sent', 1, '2025-04-05 11:59:00',
                 NULL, '2025-04-05 11:58:00', '2025-04-05 11:59:30'),
                (604, 'default', 603, 'failed', 3, '2025-04-05 12:01:00',
                 'legacy failure', '2025-04-05 12:00:30', '2025-04-05 12:02:00');
            """
        )

    copy2(
        source_migrations / "0019_delivery_revision_integrity.sql",
        migrations_dir / "0019_delivery_revision_integrity.sql",
    )
    db.migrate()
    db.migrate()

    with sqlite3.connect(database) as con:
        gallery = con.execute(
            "SELECT listing_id, published, delivered_at FROM galleries WHERE id=603"
        ).fetchone()
        notification = con.execute(
            """SELECT gallery_id, event_key, status, attempts, claimed_at, error,
                      created_at, updated_at
               FROM delivery_notifications WHERE id=604"""
        ).fetchone()
        older_notification = con.execute(
            """SELECT gallery_id, event_key, status, attempts, claimed_at, error,
                      created_at, updated_at
               FROM delivery_notifications WHERE id=599"""
        ).fetchone()
        notification_count = con.execute(
            "SELECT COUNT(*) FROM delivery_notifications WHERE studio_id='default'"
        ).fetchone()[0]
        intent_tables = {
            row[0]
            for row in con.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name IN
                     ('sms_reminder_intents','rebooking_email_intents')"""
            )
        }
        foreign_key_errors = con.execute("PRAGMA foreign_key_check").fetchall()

    assert gallery == (602, 0, "2025-04-05 12:00:00")
    assert notification == (
        603,
        "listing:602:delivered:r2",
        "failed",
        3,
        "2025-04-05 12:01:00",
        "legacy failure",
        "2025-04-05 12:00:30",
        "2025-04-05 12:02:00",
    )
    assert older_notification == (
        600,
        "listing:602:delivered:r2:legacy:599",
        "sent",
        1,
        "2025-04-05 11:59:00",
        None,
        "2025-04-05 11:58:00",
        "2025-04-05 11:59:30",
    )
    assert notification_count == 2
    assert intent_tables == {"sms_reminder_intents", "rebooking_email_intents"}
    assert foreign_key_errors == []
    assert _versions(database)[-1] == "0019"
