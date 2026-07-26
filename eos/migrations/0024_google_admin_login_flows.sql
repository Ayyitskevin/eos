-- Durable, tenant-bound Google operator-login handoffs.
--
-- OAuth state, browser nonces, and completion capabilities are random secrets.
-- Only their SHA-256 fingerprints are persisted.

CREATE TABLE google_admin_login_flows (
    id                       INTEGER PRIMARY KEY,
    studio_id                TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    user_id                  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    purpose                  TEXT NOT NULL DEFAULT 'admin_login'
                             CHECK (purpose = 'admin_login'),
    return_host              TEXT NOT NULL,
    state_hash               TEXT NOT NULL UNIQUE,
    browser_nonce_hash       TEXT NOT NULL,
    completion_token_hash    TEXT UNIQUE,
    status                   TEXT NOT NULL DEFAULT 'initiated'
                             CHECK (status IN
                                    ('initiated','claimed','completed','consumed','failed')),
    state_expires_at         INTEGER NOT NULL,
    completion_expires_at    INTEGER,
    claimed_at               INTEGER,
    completed_at             INTEGER,
    consumed_at              INTEGER,
    error                    TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_google_admin_login_flow_recovery
    ON google_admin_login_flows(status, state_expires_at, completion_expires_at);
