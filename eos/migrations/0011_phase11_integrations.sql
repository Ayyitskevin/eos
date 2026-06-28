-- Phase 11: Google Calendar sync, Dropbox ingest, per-studio platform billing.

CREATE TABLE IF NOT EXISTS studio_oauth (
    studio_id       TEXT NOT NULL,
    provider        TEXT NOT NULL,
    access_token    TEXT NOT NULL,
    refresh_token   TEXT,
    expires_at      TEXT,
    scopes          TEXT,
    account_label   TEXT,
    sync_token      TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (studio_id, provider),
    FOREIGN KEY (studio_id) REFERENCES studio(id) ON DELETE CASCADE
);

ALTER TABLE studio_profiles ADD COLUMN google_calendar_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE studio_profiles ADD COLUMN google_calendar_id TEXT NOT NULL DEFAULT 'primary';
ALTER TABLE studio_profiles ADD COLUMN dropbox_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE studio_profiles ADD COLUMN dropbox_watch_path TEXT NOT NULL DEFAULT '/Eos Ingest';
ALTER TABLE studio_profiles ADD COLUMN dropbox_default_listing_id INTEGER;

ALTER TABLE appointments ADD COLUMN google_event_id TEXT;
ALTER TABLE appointments ADD COLUMN google_synced_at TEXT;

CREATE TABLE IF NOT EXISTS dropbox_sync_state (
    studio_id       TEXT PRIMARY KEY REFERENCES studio(id) ON DELETE CASCADE,
    cursor          TEXT,
    last_scan_at    TEXT
);

CREATE TABLE IF NOT EXISTS dropbox_ingest_log (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL,
    dropbox_path    TEXT NOT NULL,
    listing_id      INTEGER,
    asset_id        INTEGER,
    status          TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','done','failed')),
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dropbox_ingest_studio ON dropbox_ingest_log(studio_id);

ALTER TABLE studio ADD COLUMN stripe_subscription_id TEXT NOT NULL DEFAULT '';
ALTER TABLE studio ADD COLUMN billing_status TEXT NOT NULL DEFAULT 'none'
    CHECK (billing_status IN ('none','trialing','active','past_due','canceled'));
ALTER TABLE studio ADD COLUMN trial_ends_at TEXT;