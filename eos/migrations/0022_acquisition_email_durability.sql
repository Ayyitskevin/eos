-- Durable, tenant-scoped claims for referral introduction and follow-up email.

CREATE TABLE acquisition_email_intents (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    email_kind      TEXT NOT NULL CHECK (email_kind IN ('intro','follow_up')),
    event_key       TEXT NOT NULL,
    to_email        TEXT NOT NULL,
    subject         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'claimed'
                    CHECK (status IN ('claimed','sent','failed','unknown')),
    attempts        INTEGER NOT NULL DEFAULT 1,
    claim_token     TEXT NOT NULL,
    claimed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at         TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, event_key)
);
CREATE UNIQUE INDEX idx_acquisition_email_active
    ON acquisition_email_intents(studio_id, client_id, email_kind)
    WHERE status IN ('claimed','unknown');
CREATE INDEX idx_acquisition_email_recovery
    ON acquisition_email_intents(studio_id, status, claimed_at);
