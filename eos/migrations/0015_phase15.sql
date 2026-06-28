-- Phase 15: low priority + ops polish

ALTER TABLE listings ADD COLUMN latitude REAL;
ALTER TABLE listings ADD COLUMN longitude REAL;
ALTER TABLE listings ADD COLUMN photographer_pay_cents INTEGER;

ALTER TABLE studio_profiles ADD COLUMN drive_time_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE studio_profiles ADD COLUMN drive_buffer_min INTEGER NOT NULL DEFAULT 30;

ALTER TABLE users ADD COLUMN is_platform_admin INTEGER NOT NULL DEFAULT 0;

ALTER TABLE studio ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0;
ALTER TABLE studio ADD COLUMN read_only INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS geocode_cache (
    address_key   TEXT PRIMARY KEY,
    latitude      REAL NOT NULL,
    longitude     REAL NOT NULL,
    cached_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id            INTEGER PRIMARY KEY,
    studio_id     TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    client_id     INTEGER REFERENCES clients(id) ON DELETE CASCADE,
    endpoint      TEXT NOT NULL UNIQUE,
    p256dh        TEXT NOT NULL,
    auth          TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mls_push_log (
    id            INTEGER PRIMARY KEY,
    studio_id     TEXT NOT NULL,
    listing_id    INTEGER,
    payload       TEXT,
    status        TEXT NOT NULL DEFAULT 'received',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);