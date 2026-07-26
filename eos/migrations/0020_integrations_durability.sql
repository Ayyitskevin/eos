-- Durable provider identity and replay-safe Google Calendar / Dropbox integration state.

CREATE TABLE google_calendar_sync_intents (
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    appointment_id  INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    event_id        TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 1,
    desired_action  TEXT NOT NULL CHECK (desired_action IN ('upsert','delete')),
    payload          TEXT NOT NULL DEFAULT '{}',
    payload_hash     TEXT NOT NULL,
    provider_bound  INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','unknown','synced','failed')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (studio_id, appointment_id),
    UNIQUE (studio_id, event_id)
);
CREATE INDEX idx_google_calendar_sync_recovery
    ON google_calendar_sync_intents(studio_id, status, updated_at);

INSERT INTO google_calendar_sync_intents
    (studio_id, appointment_id, event_id, desired_action, payload, payload_hash,
     provider_bound, status)
SELECT studio_id, id, google_event_id,
       CASE WHEN status='canceled' THEN 'delete' ELSE 'upsert' END,
       '{}', 'legacy', 1, 'synced'
FROM appointments
WHERE google_event_id IS NOT NULL AND google_event_id != '';

PRAGMA foreign_keys=OFF;
CREATE TABLE dropbox_ingest_log_v2 (
    id                  INTEGER PRIMARY KEY,
    studio_id           TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    dropbox_path        TEXT NOT NULL,
    dropbox_path_lower  TEXT NOT NULL,
    provider_file_id    TEXT NOT NULL DEFAULT '',
    provider_revision   TEXT NOT NULL DEFAULT '',
    provider_content_hash TEXT NOT NULL DEFAULT '',
    provider_key        TEXT NOT NULL,
    listing_id          INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    asset_id            INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    job_id              INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    stored              TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','ingesting','done','failed')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    claimed_at          TEXT,
    claim_token         TEXT,
    error               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, provider_key)
);
INSERT INTO dropbox_ingest_log_v2
    (id, studio_id, dropbox_path, dropbox_path_lower, provider_key, listing_id,
     asset_id, status, error, created_at, updated_at)
SELECT id, studio_id, dropbox_path, lower(dropbox_path),
       'legacy:' || id || ':' || lower(dropbox_path),
       listing_id, asset_id, status, error, created_at, created_at
FROM dropbox_ingest_log;
DROP TABLE dropbox_ingest_log;
ALTER TABLE dropbox_ingest_log_v2 RENAME TO dropbox_ingest_log;
CREATE INDEX idx_dropbox_ingest_studio
    ON dropbox_ingest_log(studio_id, created_at);
CREATE INDEX idx_dropbox_ingest_recovery
    ON dropbox_ingest_log(studio_id, status, claimed_at);
CREATE UNIQUE INDEX idx_dropbox_ingest_provider_revision
    ON dropbox_ingest_log(studio_id, provider_file_id, provider_revision)
    WHERE provider_file_id != '' AND provider_revision != '';
