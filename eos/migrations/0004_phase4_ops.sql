-- Phase 4: pre-shoot questionnaires, studio ops, gallery cover.

ALTER TABLE galleries ADD COLUMN cover_asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS questionnaires (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    listing_id      INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    token           TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','completed')),
    answers         TEXT NOT NULL DEFAULT '{}',
    sent_at         TEXT,
    completed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_questionnaires_listing ON questionnaires(listing_id);

CREATE TABLE IF NOT EXISTS inquiries (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL,
    phone           TEXT NOT NULL DEFAULT '',
    message         TEXT NOT NULL DEFAULT '',
    property_address TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);