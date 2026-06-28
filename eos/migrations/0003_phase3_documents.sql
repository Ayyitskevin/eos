-- Phase 3: proposals, contracts, email log, document timestamps.

ALTER TABLE proposals ADD COLUMN intro TEXT NOT NULL DEFAULT '';
ALTER TABLE proposals ADD COLUMN sent_at TEXT;
ALTER TABLE proposals ADD COLUMN viewed_at TEXT;
ALTER TABLE proposals ADD COLUMN accepted_at TEXT;

CREATE TABLE IF NOT EXISTS contracts (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    listing_id      INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    body_sha256     TEXT,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','sent','viewed','signed')),
    signer_name     TEXT,
    signer_ip       TEXT,
    signed_at       TEXT,
    sent_at         TEXT,
    viewed_at       TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_contracts_listing ON contracts(listing_id);

CREATE TABLE IF NOT EXISTS emails_log (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    listing_id      INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    doc_kind        TEXT NOT NULL,
    doc_id          INTEGER NOT NULL,
    to_email        TEXT NOT NULL,
    subject         TEXT NOT NULL,
    sent_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_emails_log_listing ON emails_log(listing_id);