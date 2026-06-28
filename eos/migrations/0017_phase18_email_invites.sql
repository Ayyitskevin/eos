-- Phase 18 — transactional email branding + invite-only signup

CREATE TABLE IF NOT EXISTS invite_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    label       TEXT NOT NULL DEFAULT '',
    max_uses    INTEGER,
    uses        INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_invite_codes_active ON invite_codes(active);