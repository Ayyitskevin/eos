-- Phase 14: scale & retention — RBAC, verification, onboarding, SMS, reschedule.

PRAGMA foreign_keys=OFF;

CREATE TABLE users_p14 (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    email           TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT 'operator'
                    CHECK (role IN ('owner','operator','scheduler','editor','accountant')),
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, email)
);
INSERT INTO users_p14 (id, studio_id, email, password_hash, name, role, active, created_at)
SELECT id, studio_id, email, password_hash, name, role, active, created_at FROM users;
DROP TABLE users;
ALTER TABLE users_p14 RENAME TO users;

PRAGMA foreign_keys=ON;

ALTER TABLE studio ADD COLUMN signup_verified INTEGER NOT NULL DEFAULT 1;
ALTER TABLE studio ADD COLUMN signup_verify_token TEXT;

ALTER TABLE studio_profiles ADD COLUMN onboarding_step INTEGER NOT NULL DEFAULT 0;
ALTER TABLE studio_profiles ADD COLUMN onboarding_done INTEGER NOT NULL DEFAULT 0;

ALTER TABLE email_sequences ADD COLUMN channel TEXT NOT NULL DEFAULT 'email';

CREATE TABLE IF NOT EXISTS appointment_holds (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    appointment_id  INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    client_id       INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    starts_at       TEXT NOT NULL,
    token           TEXT NOT NULL UNIQUE,
    expires_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_appt_holds_appt ON appointment_holds(appointment_id);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    delta_cents     INTEGER NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_client ON credit_ledger(client_id);

CREATE TABLE IF NOT EXISTS sms_log (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL,
    to_phone        TEXT NOT NULL,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'sent',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);