-- Phase 12: security hardening, integration observability, billing gates.

ALTER TABLE appointments ADD COLUMN external_source TEXT;

CREATE TABLE IF NOT EXISTS integration_events (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL,
    provider        TEXT NOT NULL,
    event           TEXT NOT NULL,
    detail          TEXT,
    ok              INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_integration_events_studio ON integration_events(studio_id, created_at);

ALTER TABLE studio_profiles ADD COLUMN google_last_sync_at TEXT;
ALTER TABLE studio_profiles ADD COLUMN google_last_sync_error TEXT;
ALTER TABLE studio_profiles ADD COLUMN dropbox_last_scan_at TEXT;
ALTER TABLE studio_profiles ADD COLUMN dropbox_last_scan_error TEXT;

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id              INTEGER PRIMARY KEY,
    subscription_id INTEGER NOT NULL,
    studio_id       TEXT NOT NULL,
    event           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','ok','failed')),
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_sub ON webhook_deliveries(subscription_id);