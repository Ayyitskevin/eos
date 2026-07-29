-- Server-side, revocable admin sessions.
--
-- Session cookies carry a random token (signed); only its SHA-256 fingerprint
-- is persisted. Logout and password rotation revoke rows server-side, so a
-- leaked cookie can be invalidated.

CREATE TABLE admin_sessions (
    id          INTEGER PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    studio_id   TEXT NOT NULL DEFAULT 'default',
    ip          TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  REAL NOT NULL,
    revoked_at  REAL
);

CREATE INDEX idx_admin_sessions_user ON admin_sessions(user_id, revoked_at);
